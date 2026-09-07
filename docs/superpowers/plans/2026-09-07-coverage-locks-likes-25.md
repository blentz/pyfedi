# Sub-project 25: `locks.py` and `likes.py` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close `app/shared/tasks/locks.py` (82 stmts, 15.9574%) and
`app/shared/tasks/likes.py` (117 stmts, 8.8050%) to zero missing statements and
zero partial branches, landing three production changes.

**Architecture:** Two phases with a hard boundary. Phase A (Tasks 1-7) closes
`locks.py` by transferring the harness built for `flags.py`; Phase B (Tasks
8-14) closes `likes.py`, which needs new capture for a redis publish and an
`ActivityBatch` write. Task 15 registers. **Phase B does not begin until
`locks.py`'s floor is committed.**

**Tech Stack:** pytest, respx, SQLAlchemy 2.0.52, Flask, Celery, redis.

**Spec:** `docs/superpowers/specs/2026-09-07-coverage-locks-likes-25-design.md`

## Global Constraints

- **Delete nothing the task did not create.** `claude_test` in the repository root is not this campaign's.
- Only the controller runs the full suite, one pytest session at a time, **in the foreground**.
- `pytest` exits 1 on a session timeout and `run_tests.sh` propagates it. A shell **pipeline** eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- Coverage takes the **dotted module form**: `--cov=app.shared.tasks.locks`. A path form collects nothing, writes no JSON, exits 0. The JSON lands **inside** the `pyfedi_test-runner` container; retrieve it with `podman cp` (fact 137).
- **Test counts come from collection**, never `grep -c '^def test_'`.
- Before believing any failure, run `./run_tests.sh --down`.
- **Mutations:** one at a time, targeted single-line `sed`, **dry-run without `-i` and read the produced line first**, restore with `git checkout -- app/`, then assert both an empty `git diff -- app/` and the expected `wc -l`. Paste the dry-run line and the failure output; a narrated mutation result is worth nothing.
- **A surviving mutation is information** (fact 138) — usually about the test, but at `locks.py:141`'s `online()` conjunct it is about the code. See Task 7.
- Commit with `git commit -F <file>`, never `-m`. Normal English prose.
- **No ordered assertions over database-returned rows.** Set-based or sorted. Order imposed by straight-line Python is fine.
- Every line number re-derived against the current tree, **and citations into a file your own diff touches re-derived after the diff is final** (fact 139.3).
- When you change a line, read the prose **attached** to it, not only prose elsewhere citing it (fact 139.4).
- Per fact 132, decide class-level vs instance-level monkeypatching **per call site** by reading whether the production path re-loads the object; confirm with a raising probe.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_shared_tasks_locks.py` | **Create.** All `locks.py` coverage. Transfers the `flags.py` harness. |
| `tests/test_shared_tasks_likes.py` | **Create.** All `likes.py` coverage. New capture for redis and `ActivityBatch`. |
| `tests/factories.py` | **Modify.** Add `make_banned_instance`. |
| `app/shared/tasks/locks.py:89` | **Modify.** Production change 1 (D309). |
| `app/shared/tasks/likes.py:60` | **Modify.** Production change 2 (D309). |
| `app/shared/tasks/likes.py` `vote_for_poll` | **Modify.** Production change 3 (the missing gate). |
| `coverage_floors.ini` | **Modify.** Two entries, added at two different times. |
| `tests/README.md` | **Modify.** Facts from 141. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify.** New numbers from D325, plus in-place edits. |

---

# PHASE A — `locks.py`

## Task 1: Open `tests/test_shared_tasks_locks.py` with the transferred harness

**Files:**
- Create: `tests/test_shared_tasks_locks.py`

**Interfaces:**
- Produces: `_seed(local_community=True, with_keys=False)` returning
  `SimpleNamespace(instance, user, author, community, post, reply)`;
  `_peer(domain, software)`; `_make_deliverable(s, online=True)`;
  `_follower(s, http_mock, inbox, domain)` returning `(route, instance)`;
  `_sent_activity(route, index=-1)`; `_delivered_inboxes(*routes)`.
  Tasks 2-7 consume all of these.

**Context:** `locks.py` is the sixth module with the wrappers-plus-shared-handler
shape. Transfer the harness from `tests/test_shared_tasks_flags.py:1-200`,
adapting it — read that file for the pattern before writing.

**THE KEY DIFFERENCE FROM `flags.py`.** `flags.py` delivered to instances named
in an `instance_ids` argument. `locks.py` delivers to
`community.following_instances()`, which joins `CommunityMember` — so a
recipient instance must have a **member of the community**, not merely exist.
`_follower` below builds that; a helper that only creates an `Instance` row will
produce a loop that never runs, and every delivery assertion will pass vacuously
against zero deliveries.

- [ ] **Step 1: Write the module docstring and imports**

```python
"""`lock_object` and its four wrappers -- the AP Lock and Undo senders.

`app/shared/tasks/locks.py`, 145 lines: four `@celery.task` wrappers
(`lock_post:26`, `unlock_post:41`, `lock_post_reply:56`,
`unlock_post_reply:71`) delegating to `lock_object:85`. The sixth module in
which this campaign has met the wrappers-plus-handler shape, so the harness
below is transferred from `tests/test_shared_tasks_flags.py` rather than
rebuilt.

THE `is_undo` DIMENSION IS WHAT MAKES THIS MODULE DIFFERENT. Every path exists
twice, and `@context` is deleted at THREE different sites depending on which
path runs: `:107` strips it from the Lock before nesting it in an Undo, `:122`
strips it from the Undo, and `:125` strips it from the Lock on the non-undo
local path. Assert on the SERIALIZED BYTES, never on an in-memory dict --
`locks.py` deletes IN PLACE (`del lock['@context']`), so a recorder holding
`lock` reads it back after the delete.

THE FOUR WRAPPER LOOKUPS SPLIT TWO AND TWO. `:31` and `:46` are
`session.query(Post).get(post_id)` and return None for a missing id, so the
raise arrives later at `:87` as `AttributeError`; `:61` and `:76` are
`session.query(PostReply).filter_by(id=...).one()` and raise `NoResultFound` at
the lookup. Establish each by READING -- this is the fifth module in which the
campaign has met this trap.

DELIVERY REQUIRES COMMUNITY MEMBERSHIP, NOT MERELY AN INSTANCE ROW.
`Community.following_instances()` (app/models.py:842-851) joins
`CommunityMember`, so an Instance with no member of this community is never
returned and the loop body never runs. `_follower` below creates the member;
a helper that only made an Instance would leave every delivery assertion
passing against zero deliveries.

ORDER IS NEVER ASSERTED. `following_instances()` ends in an unordered
`.distinct().all()`, so which follower comes first is a property of the query
plan. Every assertion over delivered inboxes here is set-based.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.locks import (
    lock_post, lock_post_reply, unlock_post, unlock_post_reply,
)
from tests.factories import (
    make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo

# Any globally routable literal will do -- the only property
# app/utils.py:5530 reads off it is `is_global`.
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'
```

- [ ] **Step 2: Write `_seed`, `_peer` and the autouse resolver stub**

```python
def _seed(local_community=True, with_keys=False):
    """instance, user, author, community, post, reply -- committed, in that
    binding order.

    `user` is the MODERATOR performing the lock; `author` wrote the post and
    the reply. The returned object has no field named `moderator`.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:122) hardcodes
    `instance_id=1` and the db_session teardown resets every sequence, so the
    local instance is created FIRST; a peer built before this call would take
    id 1 and leave the community's FK pointing at it.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795) is a DISJUNCTION whose
    `profile_id()` falls back to a computed default; `ap_id` alone leaves the
    community silently LOCAL (fact 112).
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'themod', local=True, with_keys=with_keys)
    author = make_user(instance, 'author', local=True)
    community = make_community('c1')
    if with_keys:
        community.private_key = user.private_key
        community.public_key = user.public_key
    post = make_post(community, author, ap_id='https://test.piefed.local/post/1')
    reply = make_post_reply(post, author, body='a reply')
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, author=author,
                           community=community, post=post, reply=reply)


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
    On a machine whose resolver hijacks NXDOMAIN into a wildcard A record,
    these tests start failing at app/utils.py:5530's `is_global` check instead.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)
```

- [ ] **Step 3: Write the delivery helpers**

```python
def _make_deliverable(s, online=True):
    """Put the community on a real peer Instance so `:89`'s gate passes.

    `Instance.online()` (app/models.py:118-119) is exactly
    `not (self.dormant or self.gone_forever)`, so `online=False` sets both.
    Returns the peer.
    """
    peer = _peer()
    if not online:
        peer.dormant = True
        peer.gone_forever = True
    s.community.instance_id = peer.id
    db.session.commit()
    return peer


def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example'):
    """A remote instance that `following_instances()` will actually return.

    THREE things are required and the third is the one that is easy to miss:
    an Instance row, an inbox, and a USER ON THAT INSTANCE WHO IS A MEMBER OF
    THE COMMUNITY. `Community.following_instances()` (app/models.py:842-851)
    joins User on `User.instance_id` and CommunityMember on
    `CommunityMember.user_id`, filtering `CommunityMember.community_id` and
    `is_banned == False`. Without the membership the query returns nothing,
    the loop at `:140` never runs, and every delivery assertion in this file
    passes against zero deliveries.

    It also filters `Instance.id != 1`, so the follower must not be the local
    instance `_seed` created first.

    Returns `(route, instance)`.
    """
    inst = make_instance(domain, software='lemmy')
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call.

    THE BYTES, NOT A DICT. `locks.py` deletes `@context` in place at `:107`,
    `:122` and `:125`, so a recorder holding the payload would be read back
    after the delete. respx captures what was handed to the transport at
    app/activitypub/signature.py:498.
    """
    return json.loads(route.calls[index].request.content)


def _delivered_inboxes(*routes):
    """The SET of inboxes that received a request. Never a list, never ordered
    -- `following_instances()` ends in an unordered `.distinct().all()`."""
    return {str(r.calls[i].request.url) for r in routes for i in range(len(r.calls))}
```

- [ ] **Step 4: Write the two smoke tests**

```python
def test_lock_post_announces_to_a_following_instance(db_session, http_mock):
    """`lock_post:26` end to end on a LOCAL community: `.get()` at `:31`, the
    gate at `:89` passing, the Lock envelope at `:95-104`, the Announce wrapper
    at `:131-139`, and delivery at `:142`.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    lock_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Lock'
    assert announce['object']['object'] == s.post.public_url()
    assert announce['actor'] == s.community.public_url()


def test_lock_post_reply_sends_a_lock_to_a_remote_community(db_session, http_mock):
    """`lock_post_reply:56` on a REMOTE community: `.one()` at `:61`, and
    `:143-145`'s else arm sending the bare Lock to the community's own inbox
    rather than wrapping it in an Announce.

    The assertion that earns this test its place is `object`: it is the
    REPLY's url, which is what distinguishes `:61`'s lookup from `:31`'s
    reaching the same handler.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    lock_post_reply(None, s.user.id, s.reply.id)

    lock = _sent_activity(route)
    assert lock['type'] == 'Lock'
    assert lock['object'] == s.reply.public_url()
    assert lock['object'] != s.post.public_url()
    assert '@context' in lock
```

- [ ] **Step 5: Run**

Run: `./run_tests.sh tests/test_shared_tasks_locks.py -v`
Expected: PASS, 2 tests.

**If the first test fails with zero deliveries**, `_follower`'s membership is
the first thing to check — read `following_instances()` and confirm every one
of its filters is satisfied. Do not "fix" it by asserting less.

- [ ] **Step 6: Commit**

Subject: `test: open tests/test_shared_tasks_locks.py with the Lock harness`

---

## Task 2: The gate at `:89` — the two arms that exist today

**Files:**
- Modify: `tests/test_shared_tasks_locks.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable`, `_follower` from Task 1.

**Context:** `:89` reads
`if community.local_only or not community.instance.online(): return`. Task 3
adds the third condition. Each early return is proved by **two** observations:
no outbound request AND `db.session.query(ActivityPubLog).count() == 0`.
`post_request` writes its row unconditionally at
`app/activitypub/signature.py:105`, before the transport and before the uri
check at `:109-111` — which does not return early either, it marks the
already-written row failure/empty uri. So a count of 0 distinguishes "the guard
returned" from "the loop ran and delivered nowhere".

- [ ] **Step 1: Write the two tests**

```python
def test_a_local_only_community_sends_no_lock(db_session, http_mock):
    """`:89`'s first disjunct.

    NO ROUTE IS REGISTERED. `http_mock` is built with `assert_all_called=True`
    (tests/conftest.py:342), so a route registered here and never called would
    fail this test for the wrong reason. The follower is built WITHOUT a route
    for the same reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    lock_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_no_lock(db_session, http_mock):
    """`:89`'s third disjunct after Task 3, its second today.

    THE CONTROL IS TASK 1's SMOKE TEST, which runs the same path with an online
    instance and DOES deliver. Without that pairing, zero rows passes against
    any breakage that stops delivery for any reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    lock_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run** — `./run_tests.sh tests/test_shared_tasks_locks.py -v`, expect 4 passed.

- [ ] **Step 3: Prove both assertions can fail**

```bash
sed -n '89p' app/shared/tasks/locks.py
sed '89s/.*/    if False:/' app/shared/tasks/locks.py | sed -n '89p'   # DRY RUN, read it
sed -i '89s/.*/    if False:/' app/shared/tasks/locks.py
./run_tests.sh tests/test_shared_tasks_locks.py -q    # both must FAIL
git checkout -- app/
git diff -- app/                      # must be empty
wc -l app/shared/tasks/locks.py       # must be 145
```

- [ ] **Step 4: Commit** — subject: `test: cover the Lock gate's local_only and offline arms`

---

## Task 3: Production change 1 — the `private` conjunct at `:89` (D309)

**Files:**
- Modify: `app/shared/tasks/locks.py:89`
- Modify: `tests/test_shared_tasks_locks.py`

**Context:** D309 is the campaign's federation-gate family: guards that test
`local_only` and `online()` but not `Community.private`, so a private
community's activity federates out. Closing `locks.py:89` drops the count from
five files to four.

- [ ] **Step 1: Write the failing test**

```python
def test_a_private_community_sends_no_lock(db_session, http_mock):
    """D309's site in this module. Before this commit `:89` gated on
    `local_only` and `instance.online()` but not `Community.private`, so a lock
    on content in a private community federated out. This test pins down that
    `community.private` is now part of the guard.

    THE ORDER OF `:89`'s DISJUNCTS IS LOAD-BEARING. `private` sits BEFORE
    `not community.instance.online()`, and `or` short-circuits left to right,
    so a private community with no instance row (`Community.instance_id`,
    app/models.py:575, is a nullable FK) returns at the `private` check instead
    of raising `AttributeError` on `None.online()`. That is a side effect of
    this fix, not something this test asserts -- reordering the disjuncts would
    reopen the crash without failing this test, since this community always has
    an instance.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    lock_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run it and READ the failure**

Expected: FAIL on `assert 1 == 0`. **Confirm it is the count assertion**, not a
fixture error, an `AttributeError` on `Community.private`, or an unmatched-route
error. A test that fails for the wrong reason proves nothing when it passes.

- [ ] **Step 3: Make the production change**

`app/shared/tasks/locks.py:89`, from:

```python
    if community.local_only or not community.instance.online():
```

to:

```python
    if community.local_only or community.private or not community.instance.online():
```

- [ ] **Step 4: Run the file** — expect 5 passed.

- [ ] **Step 5: Verify the surface**

```bash
git diff --numstat -- app/shared/tasks/locks.py   # must be: 1  1
wc -l app/shared/tasks/locks.py                    # must be 145
```

- [ ] **Step 6: Commit** (test + production change in ONE commit)

Subject: `fix: stop lock_object federating a private community`

---

## Task 4: All four wrappers — two lookup styles, `is_undo`, and teardown

**Files:**
- Modify: `tests/test_shared_tasks_locks.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable`, `_follower` from Task 1.
- Produces: `_recording_task_session(monkeypatch)` returning
  `SimpleNamespace(calls=[])`. Task 7 consumes it.

**Context:** Four wrappers, two lookup styles, and an `is_undo` flag that two
of them pass. Both raises must be **natural** — construct a missing row, never
inject an exception.

- [ ] **Step 1: Add `_recording_task_session`**

```python
def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    THE SIXTH COPY of a helper that also lives in
    tests/test_shared_tasks_send_reply.py, test_shared_tasks_send_answer.py,
    test_shared_tasks_send_post.py, test_shared_tasks_add_remove.py and
    test_shared_tasks_flags.py. Duplicated rather than imported: this campaign
    keeps its test modules independent so a helper can be edited for one
    function's needs without silently changing another's assertions. The count
    is registered as D324, which this file's existence moves from five to six.

    The session is real -- only the observation is added, by wrapping the two
    methods rather than replacing the object. A fake session would prove the
    wrapper calls methods on a mock; this proves it calls them on the session
    the function actually used.

    THE PATCH TARGET IS THE LOCKS MODULE, NOT `app.utils`.
    `app/shared/tasks/locks.py:4` imports `get_task_session` into the locks
    namespace and all four wrappers resolve it there (`:28`, `:43`, `:58`,
    `:73`). Patching `app.utils.get_task_session` would apply cleanly, observe
    nothing, and leave the assertion trivially true against an empty list.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.locks as locks_module

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

    monkeypatch.setattr(locks_module, 'get_task_session', _make)
    return record
```

- [ ] **Step 2: Write the four error tests**

Written out four times rather than parametrised: a parametrised failure names
the parameter rather than the function, and each wrapper's `except`/`finally`
is a different pair of lines.

```python
def test_a_missing_post_raises_AttributeError_and_rolls_back(
        db_session, monkeypatch):
    """`:31`'s `.get(post_id)` against an absent id, plus `:33-35`'s except arm
    and `:36-37`'s finally.

    `.get()` returns None rather than raising, so the failure arrives at `:87`
    (`object.community` on None) as `AttributeError` -- fifty-six lines later.
    Asserting `['rollback', 'close']` rather than merely `raises` is what makes
    this a test of the WRAPPER: a `raises`-only test cannot tell a rollback
    from its absence, and the ORDER distinguishes `finally` running after
    `except` from a wrapper that closed instead of rolling back.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        lock_post(None, s.user.id, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_a_missing_post_on_the_unlock_path_also_raises_AttributeError(
        db_session, monkeypatch):
    """`:46`'s `.get(post_id)` and `:48-52`'s tail. The undo twin of the test
    above, written out separately because `unlock_post`'s except and finally
    are different lines from `lock_post`'s."""
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        unlock_post(None, s.user.id, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_a_missing_reply_raises_NoResultFound_and_rolls_back(
        db_session, monkeypatch):
    """`:61`'s `.filter_by(id=...).one()` against an absent id, plus `:63-67`.

    THE DIFFERENT EXCEPTION TYPE IS THE POINT. `.one()` raises at the LOOKUP,
    so `lock_object` is never entered at all -- unlike the post wrappers, which
    reach `:87`. Asserting the type is what records the asymmetry.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(NoResultFound):
        lock_post_reply(None, s.user.id, s.reply.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_a_missing_reply_on_the_unlock_path_also_raises_NoResultFound(
        db_session, monkeypatch):
    """`:76`'s `.one()` and `:78-82`'s tail."""
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(NoResultFound):
        unlock_post_reply(None, s.user.id, s.reply.id + 1000)

    assert record.calls == ['rollback', 'close']
```

- [ ] **Step 3: Write the happy-path control**

```python
def test_lock_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """`:36-37`'s finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the four error tests: without it, `finally` running is only
    ever observed alongside an exception, and a wrapper that closed only in the
    `except` arm would pass everything else in this file.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    _follower(s, http_mock)
    record = _recording_task_session(monkeypatch)

    lock_post(None, s.user.id, s.post.id)

    assert record.calls == ['close']
```

- [ ] **Step 4: Probe that the patch is load-bearing (fact 132)**

Temporarily make `_make` raise `RuntimeError('probe')` instead of returning a
session. Run the five tests. **All five must fail with that error.** A test
that passes with a raising stub was never intercepting, and
`record.calls == [...]` would be trivially true against an empty list. Some
tests may ALSO show a respx teardown error under `assert_all_called=True` — that
is benign, but say which is which. Revert and confirm byte-identity. Paste the
actual output.

- [ ] **Step 5: Run** — expect 10 passed.

- [ ] **Step 6: Commit** — subject: `test: cover all four Lock wrappers' lookups and teardown`

---

## Task 5: The Announce wrapper and the `@context` deletions

**Files:**
- Modify: `tests/test_shared_tasks_locks.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable`, `_follower`, `_sent_activity`.

**Context:** This is the task the spec was written around. `locks.py` is the
first target since sub-project 23 with a real Announce wrapper, so the absence
of a nested `@context` is a **discriminating** assertion here — where in
`flags.py` it was vacuous.

- [ ] **Step 1: Write the four envelope tests**

```python
def test_the_announce_carries_a_top_level_context_and_a_bare_lock(
        db_session, http_mock):
    """`:125`'s `del lock['@context']` on the non-undo local path, and
    `:131-139`'s Announce.

    THIS ASSERTION DISCRIMINATES HERE AND WOULD NOT HAVE ONE SUB-PROJECT AGO.
    `app/activitypub/signature.py:100-101` reinjects `@context` TOP-LEVEL ONLY.
    In `flags.py` the Flag WAS the top-level object, so its `@context` was
    present whether or not the builder set it and no assertion could tell.
    Here the top level is the Announce and the Lock is NESTED, which the
    reinjection never reaches -- so `'@context' not in announce['object']`
    fails if `:125` stops deleting.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    lock_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert '@context' in announce
    assert '@context' not in announce['object']
    assert announce['object']['type'] == 'Lock'


def test_the_undo_announce_nests_a_context_free_undo_around_a_context_free_lock(
        db_session, http_mock):
    """The is_undo local path, where `@context` is deleted TWICE: `:107` strips
    it from the Lock before `:113` nests it in the Undo, and `:122` strips it
    from the Undo before `:135` nests THAT in the Announce.

    So the delivered object has `@context` at exactly one level out of three.
    Both inner assertions fail independently -- `:107` and `:122` are separate
    statements and a regression could drop either.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    unlock_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert '@context' in announce
    assert announce['object']['type'] == 'Undo'
    assert '@context' not in announce['object']
    assert '@context' not in announce['object']['object']
    assert announce['object']['object']['type'] == 'Lock'


def test_a_remote_lock_keeps_its_context(db_session, http_mock):
    """`:143-145`'s else arm. No Announce, so the Lock is the top-level object
    and KEEPS the `@context` set at `:100` -- `:125` never ran.

    Note what this test cannot prove: `signature.py:100-101` would reinject
    `@context` here anyway, so its presence is not evidence that `:100` set it.
    What the assertion does establish is the SHAPE -- a bare Lock rather than
    an Announce -- and `type` is what carries that.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    lock_post(None, s.user.id, s.post.id)

    lock = _sent_activity(route)
    assert lock['type'] == 'Lock'
    assert '@context' in lock


def test_a_remote_undo_wraps_a_context_free_lock(db_session, http_mock):
    """`:143-145` with is_undo: the Undo is top-level, and `:107` stripped the
    Lock's `@context` before `:113` nested it.

    THIS IS THE REMOTE PATH'S DISCRIMINATING ASSERTION. The Undo's own
    `@context` proves nothing (reinjection would supply it), but the nested
    Lock's ABSENCE is beyond the reinjection's reach and fails if `:107`
    stops deleting.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    unlock_post(None, s.user.id, s.post.id)

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Lock'
    assert '@context' not in undo['object']
```

- [ ] **Step 2: Write the audience/cc test**

```python
def test_the_announce_addresses_the_communitys_followers(db_session, http_mock):
    """`:130`'s `cc = [community.ap_followers_url]`, which REPLACES the
    `cc = [community.public_url()]` set at `:94` for the non-announce paths.

    The rebinding at `:130` is easy to miss because `cc` is built at `:94`,
    used in the Lock at `:103`, and then overwritten -- so the Lock nested
    inside the Announce carries the OLD cc while the Announce carries the new
    one. Both are asserted here.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    s.community.ap_followers_url = 'https://test.piefed.local/c/c1/followers'
    db.session.commit()

    lock_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert announce['cc'] == ['https://test.piefed.local/c/c1/followers']
    assert announce['object']['cc'] == [s.community.public_url()]
```

- [ ] **Step 3: Run** — expect 15 passed.

- [ ] **Step 4: Commit** — subject: `test: cover the Lock Announce wrapper and its nested context deletions`

---

## Task 6: The delivery loop at `:140-142`

**Files:**
- Modify: `tests/test_shared_tasks_locks.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable`, `_follower`, `_delivered_inboxes`.
- Also uses: `make_instance_block` from `tests/factories.py` (already exists,
  at `tests/factories.py:577`; re-derive) and a new `make_banned_instance`,
  added in this task.

**Context:** `:141` has four conjuncts. **Three are falsifiable; `online()` is
not** — `following_instances()` filters `dormant` and `gone_forever`, and
`Instance.online()` is exactly their negation, so every instance the loop
receives satisfies it. Do not write a skip test for `online()`; it would assert
an unreachable state. That redundancy is D302's shape and is handled in Task 7's
mutation table.

**ORDERING DISCIPLINE.** For each falsifiable conjunct, the loop must be shown
to **continue** past the skip, which needs two instances. **Create the SKIPPED
instance first** so it takes the lower id: `following_instances()` ends in an
unordered `.distinct().all()`, and on a small table Postgres returns ascending
id, so a skipped-first arrangement makes an aborting loop deliver NOTHING and
fail the test. The reverse arrangement would let an aborting loop deliver once
and pass. Say in each docstring that the residual assumption degrades to **lax**
(passes when it should fail), never to **flaky**.

- [ ] **Step 1: Add the `make_banned_instance` factory**

Append to `tests/factories.py`, and add `BannedInstances` to the `app.models`
import list if absent — check first.

```python
def make_banned_instance(domain: str) -> BannedInstances:
    """A row that makes `instance_banned(domain)` return True.

    `app/utils.py:2335` opens its OWN task session and queries
    `BannedInstances` by exact domain, then separately by a `*` wildcard
    pattern. This factory covers the exact-match arm; the wildcard arm is not
    exercised by the outbound delivery gates that call it.
    """
    banned = BannedInstances(domain=domain)
    db.session.add(banned)
    db.session.commit()
    return banned
```

- [ ] **Step 2: Write the three skip tests**

```python
def test_an_instance_without_an_inbox_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:141`'s first conjunct, with a second follower proving the loop
    CONTINUES rather than aborting.

    THE INBOXLESS FOLLOWER IS CREATED FIRST so it takes the lower id. This
    relies on Postgres returning a small unordered join in ascending id --
    an assumption about the query plan, not a guarantee. If it ever breaks
    this test degrades to LAX (it would pass under an aborting loop too), never
    to FLAKY: there is no direction in which a correct, continuing loop starts
    failing. A rigorous proof would control the order `:140` returns rows in,
    which this file does not do because those rows come from the task session.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    dud = make_instance('inboxless.example', software='lemmy')
    dud.inbox = None
    dud_member = make_user(dud, 'member_inboxless')
    make_community_member(dud_member, s.community)
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    lock_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(route) == {OTHER_INBOX}
    assert len(route.calls) == 1


def test_a_blocked_instance_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:141`'s third conjunct, `not user.has_blocked_instance(instance.id)`.

    `User.has_blocked_instance` (app/models.py:1467-1471) reads InstanceBlock
    through `db.session` -- which `patch_db_session` has pointed at the
    wrapper's task session for the duration of this call, so the row committed
    here is visible.

    Blocked follower created first, for the reason the test above states.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    blocked = make_instance('blocked.example', software='lemmy')
    blocked.inbox = PEER_INBOX
    blocked_member = make_user(blocked, 'member_blocked')
    make_community_member(blocked_member, s.community)
    make_instance_block(s.user, blocked)
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    lock_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(route) == {OTHER_INBOX}


def test_a_banned_instance_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:141`'s fourth conjunct, `not instance_banned(instance.domain)`.

    `instance_banned` (app/utils.py:2335) opens ITS OWN task session -- a third
    session live during this call, after the wrapper's and any the handler
    uses. The BannedInstances row must therefore be COMMITTED, not merely
    added, to be visible to it.

    Banned follower created first, for the reason the inbox test states.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    banned = make_instance('banned.example', software='lemmy')
    banned.inbox = PEER_INBOX
    banned_member = make_user(banned, 'member_banned')
    make_community_member(banned_member, s.community)
    make_banned_instance('banned.example')
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    lock_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(route) == {OTHER_INBOX}
```

- [ ] **Step 3: Write the multi-delivery test**

```python
def test_two_followers_both_receive_the_announce(db_session, http_mock):
    """`:140`'s loop delivering more than once -- the arc back to the top.

    Set-based, plus a per-route count so a duplicate delivery to one of the two
    cannot hide behind a matching set.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    first, _f = _follower(s, http_mock, inbox=PEER_INBOX, domain='first.example')
    second, _sec = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='second.example')

    lock_post(None, s.user.id, s.post.id)

    assert _delivered_inboxes(first, second) == {PEER_INBOX, OTHER_INBOX}
    assert len(first.calls) == 1
    assert len(second.calls) == 1
```

- [ ] **Step 4: Update the imports** — add `make_banned_instance` and
`make_instance_block` to the `tests.factories` import at the top of the file.

- [ ] **Step 5: Run** — expect 19 passed.

- [ ] **Step 6: Commit** — subject: `test: cover the Lock delivery loop's three falsifiable skips`

---

## Task 7: Close `locks.py`, mutate, set its floor — **PHASE A CHECKPOINT**

**Files:**
- Modify: `tests/test_shared_tasks_locks.py` (only if coverage shows gaps)
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Measure**

```bash
./run_tests.sh tests/test_shared_tasks_locks.py \
  --cov=app.shared.tasks.locks --cov-branch \
  --cov-report=json:/tmp/locks_cov.json
podman cp pyfedi_test-runner_1:/tmp/locks_cov.json <scratchpad>/locks_cov.json
```

Dotted form required. The JSON lands inside the container (fact 137). Read
`missing_lines` and `num_partial_branches`.

- [ ] **Step 2: Close any residual** using the helpers already in the file. For
anything genuinely unreachable, document the proof rather than writing a test
that pretends otherwise.

- [ ] **Step 3: Mutate — one at a time**

| # | Target | Mutation | Expected |
|---|---|---|---|
| M1 | `:89` | drop `community.private or` | killed by `test_a_private_community_sends_no_lock` |
| M2 | `:89` | drop `community.local_only or` | killed by `test_a_local_only_community_sends_no_lock` |
| M3 | `:89` | drop `not community.instance.online()` | killed by `test_an_offline_community_instance_sends_no_lock` |
| M4 | `:107` | delete the `del lock['@context']` line | killed by `test_the_undo_announce_nests_a_context_free_undo_around_a_context_free_lock` |
| M5 | `:125` | delete the `del lock['@context']` line | killed by `test_the_announce_carries_a_top_level_context_and_a_bare_lock` |
| M6 | `:141` | change `instance.inbox` to `True` | killed by the inbox skip test |
| M7 | `:141` | **delete `instance.online() and`** | **EXPECTED TO SURVIVE — see below** |
| M8 | `:61` | change `.filter_by(id=post_reply_id).one()` to `.get(post_reply_id)` | killed by `test_a_missing_reply_raises_NoResultFound_and_rolls_back` (wrong exception type) |

**M7 IS EXPECTED TO SURVIVE AND THAT IS THE CORRECT RESULT.** Do not
strengthen a test until it dies. `following_instances()` (`app/models.py:849-850`)
already filters `Instance.dormant == False` and `Instance.gone_forever == False`,
and `Instance.online()` (`:118-119`) is exactly `not (self.dormant or
self.gone_forever)`, so no reachable input distinguishes the mutant from the
original. Fact 138 says a surviving mutation is information about the test; this
is the case where it is information about the **code**. Record the survival with
that reading, note it as a new D302 site for Task 15, and move on. Killing it
would require asserting a state that cannot occur.

Standard instrument for M1-M6 and M8: read the line, dry-run the `sed` **without
`-i`** and confirm the output differs, apply, run, restore with
`git checkout -- app/`, then assert an empty `git diff -- app/` and
`wc -l app/shared/tasks/locks.py` == 145. Paste every dry-run line and every
failure.

- [ ] **Step 4: Add the floor**

```ini
app/shared/tasks/locks.py = 100
```

- [ ] **Step 5: Prove the floor bites** by inversion: hold floors fixed, take a
**copy** of the report, regress that copy below the floor, confirm
`tests/check_coverage_floors.py` exits non-zero naming the module. Include an
isolation control — an unrelated module regressed in the same copy must not be
misattributed. Delete the copies; they are scratch.

- [ ] **Step 6: Commit** — subject: `test: close app/shared/tasks/locks.py and set its floor`

- [ ] **Step 7: PHASE A CHECKPOINT.** Report the collected count, the coverage
figures, every mutation result including M7's survival, and confirm the floor is
committed. **Phase B does not begin until this is done.**

---

# PHASE B — `likes.py`

## Task 8: Open `tests/test_shared_tasks_likes.py`; the wrappers and `federate`

**Files:**
- Create: `tests/test_shared_tasks_likes.py`

**Interfaces:**
- Produces: `_seed(local_community=True, with_keys=False)` returning
  `SimpleNamespace(instance, user, author, community, post, reply)`;
  `_make_deliverable(s, online=True)`; `_follower(s, http_mock, inbox, domain,
  software='lemmy')` returning `(route, instance)`; `_sent_activity`;
  `_delivered_inboxes`. Tasks 9-14 consume these.

**Context:** `likes.py` shares the wrapper shape with `locks.py` only at the
surface. Read `app/shared/tasks/likes.py` in full — 236 lines — before writing.

**Copy the harness from `tests/test_shared_tasks_locks.py`**, which Phase A just
built, rather than from `flags.py`. `_follower` must create community membership
for the same reason: `send_vote` delivers via `following_instances()`.

**`_follower` GAINS A `software` PARAMETER** here, because `:141` routes
`piefed` and `pylova` instances to an `ActivityBatch` write instead of an HTTP
send. Default it to `'lemmy'` so existing-shaped callers get HTTP.

- [ ] **Step 1: Write the module docstring**

```python
"""`send_vote`, `vote_for_poll` and their wrappers -- the AP Like/Dislike path.

`app/shared/tasks/likes.py`, 236 lines. Two `@celery.task` wrappers
(`vote_for_post:24`, `vote_for_reply:40`) delegating to `send_vote:55`, plus a
third independent task `vote_for_poll:176`.

THIS MODULE HAS THREE DELIVERY MECHANISMS FOR ONE LOCAL COMMUNITY, which no
sibling module has. `:135`'s loop chooses per instance: `:141` writes an
`ActivityBatch` row for `piefed`/`pylova` peers and commits inside the loop;
`:145` appends a signed request and publishes them to redis after the loop when
`current_app.config['NOTIF_SERVER']` is truthy; `:152` sends directly
otherwise. `NOTIF_SERVER` defaults to `''` (config.py:131), so the second arm
needs a config override to reach.

THREE TASK SESSIONS ARE LIVE IN ONE `send_vote` CALL. The wrapper opens one at
`:26`/`:42` and `patch_db_session` installs it as `db.session`; `send_vote`
opens its OWN at `:56` and never patches it; and `instance_banned`
(app/utils.py:2336) opens another per call. The objects mix: `object` arrives
from the outer session while `user` at `:58` comes from the inner one, and
`user.has_blocked_instance()` (app/models.py:1467-1471) reads through
`db.session` -- the outer one. Nothing observed misbehaves because of this and
this file asserts nothing about it; it is registered as a measured mechanism.

`likes.py` COPIES BEFORE DELETING (`vote_public.copy()` at `:96` and `:110`/
`:112`, then `del`), where `locks.py` deletes IN PLACE. Assert on SERIALIZED
BYTES in both, for the reason `locks.py`'s file gives.
"""
```

- [ ] **Step 2: Write the imports and constants**

The import list must cover everything Tasks 9-13 use, not just Task 8's own
tests. Re-derive every factory's existence before importing it.

```python
import json
import socket
from types import SimpleNamespace

import pytest
from flask import current_app

from app import db
from app.models import ActivityBatch, ActivityPubLog
from app.shared.tasks.likes import vote_for_poll, vote_for_post, vote_for_reply
from tests.factories import (
    make_banned_instance, make_community, make_community_ban,
    make_community_member, make_instance, make_instance_block, make_poll,
    make_poll_choice, make_post, make_post_reply, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'
```

`make_banned_instance` was added to `tests/factories.py` by Task 6.
`make_community_ban` is at `tests/factories.py:414`, `make_instance_block` at
`:577`, `make_poll` at `:765` and `make_poll_choice` at `:783` — **re-derive all
four line numbers**, since Task 6 appended to that file and shifted anything
below its insertion point.

- [ ] **Step 3: Write the helpers**

Transfer `_seed`, `_peer`, the autouse resolver fixture, `_make_deliverable`,
`_sent_activity` and `_delivered_inboxes` from
`tests/test_shared_tasks_locks.py` verbatim. Write `_follower` with the new
parameter:

```python
def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example',
              software='lemmy'):
    """A remote instance that `following_instances()` will actually return.

    Requires an Instance row, an inbox, AND a user on that instance who is a
    member of the community -- `following_instances()` (app/models.py:842-851)
    joins CommunityMember, so without the membership the loop never runs and
    every delivery assertion passes against zero deliveries.

    `software` matters here in a way it did not for locks.py: `:141` routes
    `piefed` and `pylova` instances to an ActivityBatch write instead of an
    HTTP send, so a caller wanting an HTTP assertion must leave the default.

    Returns `(route, instance)`. The route is registered unconditionally; a
    caller expecting a BATCH rather than a send should build the instance
    directly instead, because `http_mock`'s `assert_all_called=True` fails a
    route that never fires.
    """
    inst = make_instance(domain, software=software)
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst
```

- [ ] **Step 4: Write the smoke tests and the `federate` arms**

```python
def test_vote_for_post_announces_a_like_to_a_following_instance(
        db_session, http_mock):
    """`vote_for_post:24` end to end: `.get()` at `:29`, `federate` defaulting
    True at `:30`, and the local-community Announce at `:122-130` delivered
    directly at `:152`.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Like'
    assert announce['object']['object'] == s.post.public_url()


def test_vote_for_reply_announces_a_dislike_naming_the_reply(
        db_session, http_mock):
    """`vote_for_reply:40`: `.one()` at `:45`, and `:73`'s downvote arm
    selecting `Dislike` rather than `Like`.

    Two assertions carry this test: the nested `type` (which pins `:73`'s
    else arm) and the nested `object` (which pins that `:45`'s lookup, not
    `:29`'s, reached the handler).
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_reply(None, s.user.id, s.reply.id, None, 'downvote')

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'Dislike'
    assert announce['object']['object'] == s.reply.public_url()


def test_federate_false_loads_the_post_and_sends_nothing(db_session, http_mock):
    """`:30`'s false arm -- a wrapper-level federation switch no other module
    in this package has.

    NO ROUTE IS REGISTERED, and the ActivityPubLog count is what proves the
    silence. `post_request` writes its row unconditionally at
    app/activitypub/signature.py:105, before the transport, so a count of 0
    means `send_vote` was never entered rather than that delivery failed.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote', federate=False)

    assert db.session.query(ActivityPubLog).count() == 0


def test_federate_false_on_the_reply_wrapper_also_sends_nothing(
        db_session, http_mock):
    """`:46`'s false arm. Written out separately from `:30`'s rather than
    parametrised -- they are different lines in different functions, and a
    parametrised pass would cover one under a name that does not say which.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_reply(None, s.user.id, s.reply.id, None, 'upvote', federate=False)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 5: Run** — `./run_tests.sh tests/test_shared_tasks_likes.py -v`, expect 4 passed.

- [ ] **Step 6: Commit** — subject: `test: open tests/test_shared_tasks_likes.py with the vote harness`

---

## Task 9: `send_vote`'s early returns

**Files:**
- Modify: `tests/test_shared_tasks_likes.py`

**Context:** Three early returns before any payload is built: `:60`'s gate
(D309's site, hardened in Task 10), `:63-65`'s `CommunityBan`, and `:66-68`'s
remote-community block/ban pair. Each proved by
`db.session.query(ActivityPubLog).count() == 0`, with no route registered.

- [ ] **Step 1: Write the four tests**

```python
def test_a_local_only_community_sends_no_vote(db_session, http_mock):
    """`:60`'s first disjunct."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_no_vote(db_session, http_mock):
    """`:60`'s last disjunct. The control is Task 8's smoke test, which runs
    the same path with an online instance and DOES deliver."""
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_user_banned_from_the_community_sends_no_vote(db_session, http_mock):
    """`:63-65`. `session.query(CommunityBan).filter_by(...).first()` runs on
    send_vote's OWN task session (`:56`), so the ban row must be committed --
    which `make_community_ban` does.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    make_community_ban(s.user, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_blocked_remote_community_sends_no_vote(db_session, http_mock):
    """`:66-68`'s first disjunct: a REMOTE community whose instance the voter
    has blocked.

    `:66`'s `if not community.is_local():` guards this pair, so the local
    smoke tests never reach it -- which is why this test uses
    `_seed(local_community=False)`.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_deliverable(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_banned_remote_community_sends_no_vote(db_session, http_mock):
    """`:66-68`'s second disjunct, `instance_banned(community.instance.domain)`.

    Separated from the block test above because the two conjuncts fail
    independently and a single test could not tell which one returned.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_deliverable(s)
    make_banned_instance(peer.domain)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run** — expect 9 passed.

- [ ] **Step 3: Commit** — subject: `test: cover send_vote's four early returns`

---

## Task 10: Production change 2 — the `private` conjunct at `:60` (D309)

**Files:**
- Modify: `app/shared/tasks/likes.py:60`
- Modify: `tests/test_shared_tasks_likes.py`

**Context:** D309's second site in this sub-project. Closing it drops the count
from four files to three.

- [ ] **Step 1: Write the failing test**

```python
def test_a_private_community_sends_no_vote(db_session, http_mock):
    """D309's site in this module. Before this commit `:60` gated on
    `local_only` and `instance.online()` but not `Community.private`, so a vote
    in a private community federated out.

    `private` is placed BEFORE the `online()` call for the reason locks.py's
    equivalent test states: `Community.instance_id` is a nullable FK and `or`
    short-circuits left to right, so a private community with no instance row
    returns at the guard rather than raising AttributeError. Reordering would
    reopen that without failing this test.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run and READ the failure.** Expect `assert 1 == 0`. Confirm it
is the count assertion, not a fixture fault or an `AttributeError` on
`Community.private`.

- [ ] **Step 3: Make the change**

`app/shared/tasks/likes.py:60`, from:

```python
        if community.local_only or not community.instance.online():
```

to:

```python
        if community.local_only or community.private or not community.instance.online():
```

- [ ] **Step 4: Run** — expect 10 passed.

- [ ] **Step 5: Verify** — `git diff --numstat -- app/shared/tasks/likes.py`
must be `1 1`; `wc -l app/shared/tasks/likes.py` must be 236.

- [ ] **Step 6: Commit** (test + change in ONE commit)

Subject: `fix: stop send_vote federating a private community`

---

## Task 11: The vote payload, the undo, and the remote path

**Files:**
- Modify: `tests/test_shared_tasks_likes.py`

**Context:** `:70-105` build the payload. `vote_to_undo` is a STRING (the
original type, e.g. `'Like'`) rather than a boolean — `:71` assigns it straight
to `type`. `:160-167` is the remote path.

- [ ] **Step 1: Write the payload tests**

```python
def test_an_emoji_vote_carries_its_content(db_session, http_mock):
    """`:88-89`'s `if emoji:` arm, which adds a `content` key the other paths
    never set."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote', emoji='\N{PARTY POPPER}')

    announce = _sent_activity(route)
    assert announce['object']['content'] == '\N{PARTY POPPER}'


def test_a_vote_without_an_emoji_omits_content(db_session, http_mock):
    """`:88`'s false arm. The control for the test above: without it, `content`
    being present is never distinguished from it being unconditional."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    announce = _sent_activity(route)
    assert 'content' not in announce['object']


def test_undoing_a_vote_wraps_a_context_free_copy(db_session, http_mock):
    """`:92-105`'s undo payload and `:107-115`'s local Announce around it.

    `vote_to_undo` IS A STRING, NOT A BOOLEAN: `:71` assigns it directly to
    `type`, so passing `'Like'` produces an Undo whose nested object has
    `type: 'Like'`.

    `:96-97` copies vote_public and deletes `@context` from the COPY, then
    `:110`/`:115` copies again and deletes from that. So the delivered Announce
    has `@context` at the top level only -- and the two nested absences fail
    independently, since `:97` and `:115` are separate statements.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, 'Like', 'upvote')

    announce = _sent_activity(route)
    assert '@context' in announce
    assert announce['object']['type'] == 'Undo'
    assert '@context' not in announce['object']
    assert announce['object']['object']['type'] == 'Like'
    assert '@context' not in announce['object']['object']
```

- [ ] **Step 2: Write the remote-path tests**

```python
def test_a_remote_community_receives_the_bare_vote(db_session, http_mock):
    """`:160-167`'s else arm with `vote_to_undo` None: `:165` selects
    vote_public and `:167` sends it to the community's own inbox, unwrapped.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    vote = _sent_activity(route)
    assert vote['type'] == 'Like'
    assert vote['audience'] == s.community.public_url()


def test_a_remote_community_receives_the_bare_undo(db_session, http_mock):
    """`:162-163`'s arm selecting undo_public.

    THE DISCRIMINATING ASSERTION IS THE NESTED ABSENCE. The Undo's own
    `@context` proves nothing -- signature.py:100-101 would reinject it -- but
    the nested vote's absence, deleted at `:97`, is beyond the reinjection's
    reach.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    vote_for_post(None, s.user.id, s.post.id, 'Like', 'upvote')

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Like'
    assert '@context' not in undo['object']
```

- [ ] **Step 3: Run** — expect 15 passed.

- [ ] **Step 4: Commit** — subject: `test: cover the vote payload, its undo and the remote path`

---

## Task 12: The local path's three delivery mechanisms

**Files:**
- Modify: `tests/test_shared_tasks_likes.py`

**Context:** `:135`'s loop picks one of three per instance. This is the task the
spec named as the sub-project's main risk, because mechanism 2 asserts on a
redis publish rather than an HTTP request and needs capture the campaign does
not own.

**THE REDIS CAPTURE.** `:155` does `from app import redis_client` **inside** the
function, so the name is resolved at call time and patching `app.redis_client`
before the call intercepts it. That is the idiom `tests/README.md` already
records for this client. Patch it with a double whose `publish(channel, payload)`
appends to a list, and assert on what was published — not merely that publish
was called.

- [ ] **Step 1: Write the batch-path test**

```python
def test_a_piefed_follower_is_batched_rather_than_sent(db_session, http_mock):
    """`:141-143`. A `piefed` peer gets an ActivityBatch row and a commit
    inside the loop, and NO HTTP request.

    NO ROUTE IS REGISTERED for this instance, and that is the point: under
    `http_mock`'s `assert_all_called=True` a registered-but-unfired route would
    fail this test for the wrong reason, while an unexpected SEND would surface
    as an unmatched request. So the assertion pair is "one batch row" and "zero
    ActivityPubLog rows".

    The batch payload is `payload_copy` -- the context-free inner object from
    `:115`, not the Announce -- which is what `assert '@context' not in`
    pins down.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('piefed.example', software='piefed')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_piefed')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    batches = db.session.query(ActivityBatch).all()
    assert len(batches) == 1
    assert batches[0].instance_id == inst.id
    assert batches[0].community_id == s.community.id
    assert batches[0].payload['type'] == 'Like'
    assert '@context' not in batches[0].payload
    assert db.session.query(ActivityPubLog).count() == 0


def test_a_pylova_follower_is_batched_too(db_session, http_mock):
    """`:141`'s second literal. Written out separately from the piefed test
    because `or` short-circuits: a mutation deleting the `pylova` comparison
    would leave the piefed test passing."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('pylova.example', software='pylova')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_pylova')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert db.session.query(ActivityBatch).count() == 1
```

- [ ] **Step 2: Write the redis-path test**

```python
def test_a_notif_server_publishes_to_redis_instead_of_sending(
        db_session, http_mock, monkeypatch):
    """`:145-149` and `:154-159`. With NOTIF_SERVER set, the announce is signed
    into `send_async` and published to redis after the loop rather than sent
    over HTTP.

    THE CAPTURE. `:155` does `from app import redis_client` INSIDE the
    function, so the name resolves at call time and patching `app.redis_client`
    before the call intercepts. A double recording `(channel, payload)` lets
    this assert on WHAT was published; asserting merely that publish was called
    would pass against a payload with the wrong urls in it.

    NO ROUTE IS REGISTERED -- the whole point is that nothing goes out over
    HTTP -- so an accidental send surfaces as an unmatched request.
    """
    published = []

    class _Redis:
        def publish(self, channel, payload):
            published.append((channel, payload))

    monkeypatch.setattr('app.redis_client', _Redis())
    monkeypatch.setitem(current_app.config, 'NOTIF_SERVER', 'notifs.example')

    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert len(published) == 1
    channel, payload = published[0]
    assert channel == 'http_posts:activity'
    body = json.loads(payload)
    assert body['urls'] == [PEER_INBOX]
    assert json.loads(body['data'])['type'] == 'Announce'


def test_no_notif_server_sends_directly(db_session, http_mock, monkeypatch):
    """`:150-152`'s else arm, and the control for the test above: without it,
    a direct send is never distinguished from an unconditional one.

    `NOTIF_SERVER` defaults to `''` (config.py:131), so this is the default
    path -- but set it explicitly rather than relying on the default, so the
    test still means what it says if the default ever changes.
    """
    monkeypatch.setitem(current_app.config, 'NOTIF_SERVER', '')

    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert _delivered_inboxes(route) == {PEER_INBOX}
```

- [ ] **Step 3: Write the loop's skip tests**

`:136-139` inverts `locks.py:141`'s guard into a `continue`. The same three
conjuncts are falsifiable and `online()` is not, for the same reason. **Create
the skipped instance first**, and state the lax-not-flaky direction.

```python
def test_a_follower_without_an_inbox_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:136`'s `instance.inbox` conjunct, inverted into a `continue` at `:139`.

    The inboxless follower is created FIRST so it takes the lower id and is
    returned first by `following_instances()`' unordered `.distinct().all()`.
    If that query-plan assumption ever breaks this test degrades to LAX -- it
    would pass under a loop that aborted on the skip -- never to FLAKY.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    dud = make_instance('inboxless.example', software='lemmy')
    dud.inbox = None
    dud_member = make_user(dud, 'member_inboxless')
    make_community_member(dud_member, s.community)
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert _delivered_inboxes(route) == {OTHER_INBOX}
    assert len(route.calls) == 1


def test_a_blocked_follower_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:137`'s `not user.has_blocked_instance(instance.id)`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    blocked = make_instance('blocked.example', software='lemmy')
    blocked.inbox = PEER_INBOX
    blocked_member = make_user(blocked, 'member_blocked')
    make_community_member(blocked_member, s.community)
    make_instance_block(s.user, blocked)
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert _delivered_inboxes(route) == {OTHER_INBOX}


def test_a_banned_follower_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:138`'s `not instance_banned(instance.domain)`, which opens its own
    task session at app/utils.py:2336 -- so the BannedInstances row must be
    committed to be visible to it."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    banned = make_instance('banned.example', software='lemmy')
    banned.inbox = PEER_INBOX
    banned_member = make_user(banned, 'member_banned')
    make_community_member(banned_member, s.community)
    make_banned_instance('banned.example')
    db.session.commit()
    route, _good = _follower(s, http_mock, inbox=OTHER_INBOX,
                             domain='good.example')

    vote_for_post(None, s.user.id, s.post.id, None, 'upvote')

    assert _delivered_inboxes(route) == {OTHER_INBOX}
```

- [ ] **Step 4: Add `from flask import current_app` and `ActivityBatch` to the
imports.** Re-derive the import block after your edits are final.

- [ ] **Step 5: Run** — expect 23 passed.

- [ ] **Step 6: Commit** — subject: `test: cover the vote path's three delivery mechanisms`

---

## Task 13: `vote_for_poll`, and production change 3 — the missing gate

**Files:**
- Modify: `app/shared/tasks/likes.py` (`vote_for_poll`)
- Modify: `tests/test_shared_tasks_likes.py`

**Context:** `vote_for_poll:176` opens its own session at `:177`, never patches
it, does not open `with current_app.app_context()`, and — the point of this task
— **has no federation gate at all**. `:181`'s `if post:` is its only guard. It
federates a poll vote out of a local-only or private community today with no
check whatsoever.

This is not a D309 omission. Those cells describe guards that exist and are
missing a conjunct. This is the guard's **absence**, and it gets its own number.

- [ ] **Step 1: Write the coverage tests for the paths that exist**

```python
def test_a_poll_vote_announces_to_a_following_instance(db_session, http_mock):
    """`vote_for_poll:176` on a local community: `:199`'s is_local arm,
    `:203`'s `del payload['@context']`, and delivery at `:227`.

    The nested absence is the discriminating assertion, for the reason this
    file's other Announce tests give.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    route, _inst = _follower(s, http_mock)

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'PollVote'
    assert announce['object']['choice_text'] == 'yes'
    assert '@context' not in announce['object']


def test_a_remote_poll_vote_is_sent_bare(db_session, http_mock):
    """`:229-230`'s else arm."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    payload = _sent_activity(route)
    assert payload['type'] == 'PollVote'
    assert '@context' in payload


def test_an_absent_post_votes_nowhere(db_session, http_mock):
    """`:181`'s false arm -- the only guard this function has today.

    `.get()` at `:179` returns None for an absent id, so `if post:` is the
    whole of its protection.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)

    vote_for_poll(None, s.user.id, s.post.id + 1000, 'yes')

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Write the failing test for the missing gate**

```python
def test_a_poll_vote_in_a_private_community_federates_nowhere(
        db_session, http_mock):
    """The gate `vote_for_poll` did not have.

    Every other sender in this package gates on the community before
    federating. `vote_for_poll` checked only `if post:` and then federated a
    poll vote out of a private or local-only community with no check at all.
    This is not a D309 omission -- those are guards missing a conjunct -- it is
    the guard's absence.

    This test FAILS before the gate is added: a real ActivityPubLog row is
    written and a real request attempted.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_poll_vote_in_a_local_only_community_federates_nowhere(
        db_session, http_mock):
    """The same gate's first disjunct. Separated from the private test because
    the two fail independently and one test could not say which returned."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_poll_vote_to_an_offline_instance_federates_nowhere(
        db_session, http_mock):
    """The same gate's third disjunct."""
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 3: Run and READ the failures.** All three new tests must fail on
the count assertion. If any fails for another reason — a missing `Poll` row, a
fixture fault — fix that first.

- [ ] **Step 4: Make the production change**

Inside `if post:`, after the `user` load and before the envelope is built, add:

```python
            community = post.community
            if community.local_only or community.private or not community.instance.online():
                return
```

**RE-DERIVE THE INSERTION POINT AND INDENTATION FROM THE CURRENT TREE.** This is
an INSERTION of three lines, so the file becomes 239 lines, not 236. Confirm the
file still ends with a trailing newline — `wc -l` under-reports a file without
one, and an earlier sub-project was misled by exactly that.

**EVERY LATER MUTATION RESTORE MUST ASSERT 239.**

- [ ] **Step 5: Run** — expect 29 passed.

- [ ] **Step 6: Verify the surface**

```bash
git diff --numstat -- app/shared/tasks/likes.py   # 1 1 from Task 10, plus 3 0 here
wc -l app/shared/tasks/likes.py                    # must be 239
```

- [ ] **Step 7: Fix any citation your own insertion invalidated.** The three
added lines shift every `likes.py` line below the insertion point. Re-derive
every `app/shared/tasks/likes.py:NN` citation in this test file — by grep, not
arithmetic — **after** the edit is final. A previous sub-project shipped a
citation broken by the commit that wrote it.

- [ ] **Step 8: Commit** (tests + change in ONE commit)

Subject: `fix: gate vote_for_poll on the community before federating`

---

## Task 14: Close `likes.py`, mutate, set its floor

**Files:**
- Modify: `tests/test_shared_tasks_likes.py`
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Add `_recording_task_session` for this module**

`app/shared/tasks/likes.py:5` imports `get_task_session` into the likes
namespace, so that is the patch target. **Re-derive the line numbers in the
docstring** — Task 13 inserted three lines and moved everything below them.

```python
def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    The SEVENTH copy of this helper across the suite. Duplicated rather than
    imported: this campaign keeps its test modules independent so a helper can
    be edited for one function's needs without silently changing another's
    assertions. The count is registered as D324.

    THE PATCH TARGET IS THE LIKES MODULE, NOT `app.utils`. `likes.py:5` imports
    `get_task_session` into the likes namespace and every function resolves it
    there. Patching `app.utils.get_task_session` would apply cleanly, observe
    nothing, and leave the assertions trivially true against an empty list.

    NOTE WHAT THIS DOES AND DOES NOT REACH IN THIS MODULE. `send_vote` opens
    its OWN session at `:56`, which this patch intercepts, AND runs inside a
    wrapper whose session it also intercepts -- so a `send_vote` failure
    records TWO closes, one per session. Assert on the ORDER and the presence
    of 'rollback', not on an exact list length, unless you have read which
    sessions a given path opens.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.likes as likes_module

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

    monkeypatch.setattr(likes_module, 'get_task_session', _make)
    return record
```

- [ ] **Step 2: Probe that the patch intercepts (fact 132)**

Temporarily make `_make` raise `RuntimeError('probe')` instead of returning a
session. Run the file. Tests that call any of the three entry points **must**
fail with that error. A test that passes with a raising stub was never
intercepting, and `record.calls == [...]` would be trivially true against an
empty list. Revert, confirm byte-identity, and paste the actual output.

- [ ] **Step 3: Cover the two outer handlers**

Both handlers end in a bare `except:` followed by rollback and re-raise, then a
`finally` that closes. Written out separately per function rather than
parametrised — the duplication is deliberate and pre-ruled by this campaign,
and the two `except`/`finally` pairs are different lines in different functions.

```python
def test_a_database_failure_in_vote_for_poll_rolls_back_and_re_raises(
        db_session, http_mock, monkeypatch):
    """`vote_for_poll`'s bare `except:` arm and its `finally`, reached WITHOUT
    a faked exception in the task's own logic.

    The session's `execute` is made to raise. VERIFY BY TRACEBACK where it
    first fires rather than assuming: under sqlalchemy~=2.0.0,
    `session.query(Model).get(id)` funnels through `Session.execute()` on a
    fresh Session's identity-map miss, so the raise may land at the FIRST
    lookup rather than at any later statement. Write the docstring to match
    what you measure, not what you expect.

    The recorded ORDER fixes `finally` running after `except`, which a
    `raises`-only test cannot observe.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.likes as likes_module

    s = _seed(with_keys=True)
    _make_deliverable(s)
    calls = []

    def _make():
        session = _Session(bind=_db.engine)
        real_rollback, real_close = session.rollback, session.close

        def rollback():
            calls.append('rollback')
            return real_rollback()

        def close():
            calls.append('close')
            return real_close()

        def execute(*_args, **_kwargs):
            raise RuntimeError('database refused the query')

        session.rollback = rollback
        session.close = close
        session.execute = execute
        return session

    monkeypatch.setattr(likes_module, 'get_task_session', _make)

    with pytest.raises(RuntimeError):
        vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert calls == ['rollback', 'close']


def test_vote_for_poll_closes_its_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """The same `finally` on the SUCCESS path -- `close` with no `rollback`.

    The control for the test above: without it, `finally` running is only ever
    observed alongside an exception, and a handler that closed only in the
    `except` arm would pass everything else in this file.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    make_poll(s.post)
    make_poll_choice(s.post, 'yes')
    _follower(s, http_mock)
    record = _recording_task_session(monkeypatch)

    vote_for_poll(None, s.user.id, s.post.id, 'yes')

    assert record.calls == ['close']


def test_a_failure_inside_send_vote_rolls_back_and_re_raises(
        db_session, http_mock, monkeypatch):
    """`send_vote`'s bare `except:` at the end of its body, reached by a
    NATURAL raise: the post is absent, so `:59`'s `object.community` raises
    AttributeError on None.

    TWO SESSIONS ARE RECORDED HERE and that is the observation. The wrapper
    opens one and `send_vote` opens its own at `:56`, both intercepted by the
    same patch, so a failure inside `send_vote` rolls back and closes the inner
    session and then the outer one. Asserting the exact sequence is what
    distinguishes that from a single-session teardown.

    MEASURE THE SEQUENCE BEFORE ASSERTING IT. Run the test with a bare
    `assert record.calls == []` first, read what the failure reports, and write
    that. Do not guess the order from this docstring.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        vote_for_post(None, s.user.id, s.post.id + 1000, None, 'upvote')

    assert 'rollback' in record.calls
    assert record.calls[-1] == 'close'
```

- [ ] **Step 4: Measure**

```bash
./run_tests.sh tests/test_shared_tasks_likes.py \
  --cov=app.shared.tasks.likes --cov-branch \
  --cov-report=json:/tmp/likes_cov.json
podman cp pyfedi_test-runner_1:/tmp/likes_cov.json <scratchpad>/likes_cov.json
```

- [ ] **Step 5: Close any residual.** Document a proof for anything genuinely
unreachable rather than writing a test that cannot fail.

- [ ] **Step 6: Mutate — one at a time**

| # | Target | Mutation | Expected |
|---|---|---|---|
| M9 | `:60` | drop `community.private or` | killed by `test_a_private_community_sends_no_vote` |
| M10 | `:63-65` | change `if banned:` to `if False:` | killed by `test_a_user_banned_from_the_community_sends_no_vote` |
| M11 | `:73` | swap `'Like'` and `'Dislike'` | killed by the upvote and downvote smoke tests |
| M12 | `:97` | delete `del vote_public_copy['@context']` | killed by `test_undoing_a_vote_wraps_a_context_free_copy` |
| M13 | `:141` | change the software test to `if False:` | killed by `test_a_piefed_follower_is_batched_rather_than_sent` |
| M14 | `:145` | change `if current_app.config['NOTIF_SERVER']:` to `if False:` | killed by `test_a_notif_server_publishes_to_redis_instead_of_sending` |
| M15 | the gate added in Task 13 | drop `community.private or` | killed by `test_a_poll_vote_in_a_private_community_federates_nowhere` |
| M16 | `:136` | **delete `instance.online() and`** | **EXPECTED TO SURVIVE — same reason as Task 7's M7** |

M16 survives for the reason M7 does: `following_instances()` already filters
`dormant` and `gone_forever`. Record the survival as evidence of D302's
redundancy; do not strengthen a test until it dies.

Standard instrument throughout: read the line, dry-run without `-i` and confirm
the output differs, apply, run, restore, then assert an empty
`git diff -- app/` and `wc -l app/shared/tasks/likes.py` == **239**.

- [ ] **Step 7: Add the floor** — `app/shared/tasks/likes.py = 100`

- [ ] **Step 8: Prove it bites** by inversion with an isolation control; delete
the scratch copies.

- [ ] **Step 9: Commit** — subject: `test: close app/shared/tasks/likes.py and set its floor`

---

## Task 15: Register the findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

**Context:** Next free number is **D325** — confirm against the register. Facts
end at **140**; append from **141**.

- [ ] **Step 1: Add the new entries**

- **`send_vote`'s nested unpatched session.** Three task sessions live in one
  call: the wrapper's (installed as `db.session` by `patch_db_session`),
  `send_vote`'s own at `:56`, and one per `instance_banned` call at
  `app/utils.py:2336`. Objects mix — `object` from the outer session, `user` at
  `:58` from the inner, and `has_blocked_instance` reading `db.session`.
  **Mechanism measured, no consequence claimed** — the discipline D311, D312 and
  D314's cells state. Verify each citation before writing it.
- **The two bare `except:` clauses** at `likes.py:168` and the `vote_for_poll`
  equivalent (re-derive the second; Task 13's insertion moved it). A bare except
  catches `BaseException`, so `KeyboardInterrupt` and `SystemExit` are swallowed
  into a rollback-and-re-raise path. **Say explicitly how this differs from
  D315**, which is about what is *raised* rather than what is *caught*, so the
  two families are not merged by a later reader.
- **`ActivityBatch.source_type` and `source_id` are written by nobody and read
  by nobody.** `likes.py:142` and `app/activitypub/routes.py:1992` are the only
  writers and both omit them; `app/cli.py:1132` reads by `instance_id` +
  `community_id`. The column comment at `app/models.py:4337` describes an
  undo-by-`source_id` lookup implemented nowhere. **The consequence is benign
  and the entry must say so:** the comment's own fallback — "if not found,
  federate the undo" — is what always happens, so batched votes degrade to
  correct behaviour. A dead design, not a live defect.
- **`vote_for_poll`'s missing gate** — fixed by production change 3. Record that
  it was the guard's absence rather than a D309-style omission.

- [ ] **Step 2: Make the in-place edits**

- **D309** drops from five files to **three**. State the counting unit — the
  cell counts by FILE and `deletes.py` contributes two lines at one site.
  Re-derive the remaining list.
- **D302** gains `locks.py:141` and `likes.py:136` and the `vote_for_poll`
  equivalent as sites where `instance.online()` is redundant against
  `following_instances()`' own filter. **Record that the mutations deleting
  those conjuncts SURVIVED and that the survival is the correct result** — it is
  evidence of the redundancy, and this is the case where a surviving mutation is
  information about the code rather than about the test. Re-derive the existing
  site count before writing a new one.
- **D314** gains `likes.py`'s two carriers at the strength Step 1 sets.
- **D317** gains `locks.py` and `likes.py` as the fifth and sixth modules
  carrying the `.get()`/`.one()` asymmetry.
- **D324** becomes **six** copies of `_recording_task_session`, not five.

- [ ] **Step 3: Append the harness facts from 141**

- **141** — `Community.following_instances()` joins `CommunityMember`, so a test
  fixture giving a recipient only an `Instance` row and an inbox produces a loop
  that never runs and delivery assertions that pass against zero deliveries.
  The recipient needs a USER on that instance who is a MEMBER of the community.
  It also filters `Instance.id != 1`, so the follower must not be the local
  instance.
- **142** — a redundant conjunct can be invisible to branch coverage and still
  visible to mutation testing. `coverage.py` does not decompose a conjunction —
  `if a and b and c:` has one arc pair — so an input failing any conjunct covers
  the False arc and the module reaches 100%. But a mutation deleting a redundant
  conjunct SURVIVES, because no reachable input distinguishes it. When that
  happens, check whether the conjunct is genuinely redundant before treating the
  survival as a test gap: fact 138's rule has this exception.
- **143** — `likes.py:155` does `from app import redis_client` inside the
  function, so patching `app.redis_client` before the call intercepts it. Assert
  on what was published, not that publish was called.

- [ ] **Step 4: Verify every citation you wrote** — open the line, confirm it
carries the claimed statement. Re-derive citations into files your own diff
touches after the diff is final.

- [ ] **Step 5: Commit** — subject: `docs: register sub-project 25's findings and harness facts`

---

## Final verification (controller only)

- [ ] Full suite, **foreground and unpiped**:

```bash
./run_tests.sh -q > <scratchpad>/final25.log 2>&1
echo "REAL PYTEST EXIT=$?"
tail -1 <scratchpad>/final25.log
```

- [ ] Collected counts from collection, not grep, for both new files.
- [ ] `git diff --numstat <base>..HEAD -- app/` — expect `1 1` for `locks.py` and
  `4 1` for `likes.py` (one line changed at `:60`, three inserted in
  `vote_for_poll`).
- [ ] `wc -l app/shared/tasks/locks.py app/shared/tasks/likes.py` — 145 and 239.
- [ ] `python3 tests/check_coverage_floors.py <report> coverage_floors.ini` — all
  14 floors met, and **read the report mtime it prints**. A `-q` run writes no
  coverage, so a floors check after one re-reads the previous report.
- [ ] `git status --short` — only `?? claude_test`, which is not this campaign's.

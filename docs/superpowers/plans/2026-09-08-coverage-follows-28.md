# Sub-project 28: closing `app/shared/tasks/follows.py` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `app/shared/tasks/follows.py` from 16.84% to 100% statement and branch coverage, land three production changes, and close the two near-miss residuals in `notes.py` and `pages.py`.

**Architecture:** One new test file, `tests/test_shared_tasks_follows.py`, plus additions to two existing files for the residuals. Unlike the four modules closed before it, `follows.py` has no fan-out: each task sends at most one request. Its behaviour is *state* — join-request rows created, read for a `uuid`, and deleted — so tests assert on rows as well as on the wire.

**Tech Stack:** pytest, respx, SQLAlchemy 2.0.52, Celery with `task_always_eager`, podman-compose via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-08-coverage-follows-28-design.md`

## Global Constraints

- **Delete nothing the task did not create.** No `rm`, no file removal of any kind. `git checkout -- app/` is permitted ONLY as a mutation restore step.
- **There is NO host Python with flask or pytest.** Everything runs through the podman stack: `./run_tests.sh <path> -q`.
- **Only the controller runs the full suite**, in the FOREGROUND. Task implementers run only the files they change.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted module form** (`--cov=app.shared.tasks.follows`). A path form collects nothing, writes no JSON, exits 0 — a silent green failure.
- **The coverage JSON lands INSIDE the `pyfedi_test-runner` container.** Retrieve with `podman cp`.
- **Test counts come from pytest's own collection output**, never `grep -c '^def test_'`.
- **Before believing any failure, run `./run_tests.sh --down`.** The suite is sensitive to accumulated database state across consecutive runs.
- **Mutations:** one at a time, dry-run WITHOUT `-i` and read the produced line first, apply, run, restore with `git checkout -- app/`, then assert an empty `git diff -- app/` and the expected `wc -l`. **Restore before any point where you might stop and report.** Paste every dry-run line and every result.
- **Commit with `git commit -F <file>`, never `-m`.** Normal English prose, lowercase `type:` subject prefix.
- End every commit message with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` then
  `Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn`
- **No ordered assertions over rows a query planner returned.** Order imposed by straight-line Python is fine.
- **Every line number re-derived against the current tree**, and citations into a file your own diff touches re-derived AFTER the diff is final.
- **Where prose describes a statement, cite that statement's line, not the `if` guarding it.** Harness fact 152 records five violations of this rule in the previous sub-project, every one caught by a reader rather than by the rule.
- **A top-level `@context` assertion is vacuous.** `app/activitypub/signature.py:100-101` reinjects at the top level only; only a NESTED absence discriminates.
- **A spurious send is invisible to a delivered-inboxes set and to a call count.** `signature.py:143`'s `except Exception as e:` swallows respx's unmatched-request assertion into an `ActivityPubLog` failure row. Where a test needs "nothing was sent", the oracle is a row count. `post_request` writes its row unconditionally at `signature.py:105`, before the transport.
- **Fact 153: a task writes through its own `Session`.** `get_task_session()` returns `Session(bind=db.engine)` with its own identity map, and nothing expires the object the test already holds. Call `db.session.expire_all()` before asserting on an ORM object's ATTRIBUTES. Row counts through a fresh query are immune.

## Two harness facts this module forces, which no earlier sub-project hit

**1. `flash()` needs a request context, and the `app` fixture does not provide one.** `tests/conftest.py:112` pushes only `application.app_context()`. Three of `join_community`'s early-return arms call `flash()` (`:48`, `:65`, `:95`). A test exercising those arms must push one itself:

```python
with current_app.test_request_context('/'):
    join_community(None, s.user.id, s.community.id, SRC_WEB)
```

**2. Pushing a request context DISABLES `patch_db_session`.** `app/utils.py:3685` is `if has_request_context(): yield; return` — inside a request context the helper does not patch `db.session` at all. So `SRC_WEB` tests run with `db.session` unpatched while the task uses its own session. Assert through fresh queries (`db.session.query(X).count()`), never on attributes of objects the task touched, and expect no `expire_all()` to help you there.

Both belong in the register at Task 12.

## File Structure

**Create:** `tests/test_shared_tasks_follows.py` — the whole module's tests.

**Modify:** `app/shared/tasks/follows.py` — three production changes. The file is 277 lines now; each change adds one line, so it ends at 280.

**Modify:** `tests/test_shared_tasks_send_reply.py` — one test closing `notes.py:100-101`.

**Modify:** `tests/test_shared_tasks_send_post.py` — two or three tests closing `pages.py:107-108` and its untaken branch arms; `:270` and `:333` share one condition and one test, and `:312`'s arm may prove unreachable.

**Modify:** `coverage_floors.ini` — `follows.py = 100` added; `notes.py` 99 → 100; `pages.py` 98 → 100. 19 entries.

**Modify:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` and `tests/README.md`.

---

## Task 1: Open the test file with the follows harness

**Files:**
- Create: `tests/test_shared_tasks_follows.py`

**Interfaces:**
- Produces: `_seed(local_community=True, with_keys=False)`, `_make_online(s)`, `_peer_route(http_mock, inbox=PEER_INBOX)`, `_sent_activity(route, index=-1)`, constants `PEER_INBOX`, `OTHER_INBOX`.

- [ ] **Step 1: Write the module docstring and harness**

```python
"""The five follow and unfollow tasks -- AP Follow and Undo senders.

`app/shared/tasks/follows.py`, 277 lines. Five `@celery.task` functions:
`join_community:39`, `leave_community:112`, `leave_feed:161`,
`follow_user:214` and `unfollow_user:242`.

NO FAN-OUT ANYWHERE IN THIS MODULE, which is what separates it from every
module this campaign has closed since sub-project 24. Each task sends AT MOST
ONE request, directly, to a single actor's inbox. There is no
`following_instances()`, no recipient guard, no Announce, no `domains_sent_to`.

WHAT IT HAS INSTEAD IS STATE. `CommunityJoinRequest`, `FeedJoinRequest` and
`UserFollowRequest` rows are created, read for their `uuid`, and deleted, and
the module's defects are all in the ordering of those operations against
`session.commit()`. A test that asserts only on the wire misses half of what
each task does: assert the row AND the request.

`join_community` FORKS THREE WAYS ON `src` INSIDE EACH OF TWO GUARDS.
`SRC_WEB` flashes and returns None, `SRC_PLD` returns a dict, `SRC_API` raises.
Under `task_always_eager` the wrapper returns the value directly (fact 146), so
the return value is observable -- and it is the only thing distinguishing two
of the three arms.

`flash()` NEEDS A REQUEST CONTEXT AND THE `app` FIXTURE DOES NOT PUSH ONE.
tests/conftest.py:112 pushes only `application.app_context()`. A test touching
a `SRC_WEB` arm must push its own request context -- and doing so DISABLES
`patch_db_session`, because app/utils.py:3685 returns early inside a request
context. Those tests must assert through fresh queries, never on attributes of
objects the task touched.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from flask import current_app

from app import db
from app.constants import SRC_API, SRC_PLD, SRC_WEB
from app.models import ActivityPubLog, CommunityJoinRequest, UserFollowRequest
from app.shared.tasks.follows import (
    follow_user, join_community, leave_community, leave_feed, unfollow_user,
)
from tests.factories import (
    make_community, make_community_join_request, make_feed, make_instance,
    make_user, make_user_follow_request,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo
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
    On a machine whose resolver hijacks NXDOMAIN into a wildcard A record,
    these tests start failing at app/utils.py:5530's `is_global` check instead.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _seed(local_community=True, with_keys=False):
    """instance, user, community -- committed.

    `user` is the person joining or leaving. `join_community:88` and
    `leave_community:153` sign with the USER's key, so `with_keys=True` is
    required for any test that reaches a send.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'joiner', local=True, with_keys=with_keys)
    community = make_community('c1')
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community)


def _make_online(s, online=True):
    """Move the community onto a real peer Instance and set its state.

    `join_community:74` and `leave_community:129` both dereference
    `community.instance`, so a community left on instance 1 -- which
    `make_instance` gives no inbox -- reaches those checks with the local row.
    `Instance.online()` is exactly `not (self.dormant or self.gone_forever)`
    (app/models.py:118-119). Returns the peer.
    """
    peer = make_instance('peer.example', software='lemmy')
    peer.inbox = PEER_INBOX
    if not online:
        peer.dormant = True
        peer.gone_forever = True
    s.community.instance_id = peer.id
    db.session.commit()
    return peer


def _peer_route(http_mock, inbox=PEER_INBOX):
    """A respx route for a delivery this test EXPECTS to happen.

    Do not register one for a test asserting nothing is sent: `http_mock` uses
    `respx.mock(assert_all_called=True)` (tests/conftest.py:342), so a
    registered route that never fires fails the test for the wrong reason.
    """
    return http_mock.post(inbox).respond(200, json={})


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call.
    """
    return json.loads(route.calls[index].request.content)
```

- [ ] **Step 2: Write the two smoke tests**

```python
def test_joining_a_remote_community_sends_a_follow(db_session, http_mock):
    """`join_community:74-89` end to end: the remote-and-online arm, the
    `CommunityJoinRequest` written at `:76`, and the Follow built at `:80-87`.

    THE ROW AND THE REQUEST ARE BOTH ASSERTED. `:75-77` writes the join
    request and `:88` sends the Follow; a test checking only one of the two
    would pass with the other silently broken, and the `uuid` written at `:76`
    is what `:79` puts in the activity id.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_online(s)
    route = _peer_route(http_mock)

    join_community(None, s.user.id, s.community.id, SRC_API)

    assert db.session.query(CommunityJoinRequest).count() == 1
    follow = _sent_activity(route)
    assert follow['type'] == 'Follow'
    assert follow['actor'] == s.user.public_url()
    assert follow['object'] == s.community.public_url()


def test_following_a_remote_user_sends_a_follow(db_session, http_mock):
    """`follow_user:214-237`. A `UserFollowRequest` at `:219-221`, then one
    Follow to the target's own inbox at `:230`.

    Signed with the FOLLOWER's key, not the target's -- `:230` passes
    `user.private_key`, where `user` is the follower loaded at `:218`.
    """
    s = _seed(with_keys=True)
    peer = make_instance('peer.example', software='lemmy')
    target = make_user(peer, 'target')
    target.ap_inbox_url = PEER_INBOX
    db.session.commit()
    route = _peer_route(http_mock)

    follow_user(target.id, s.user.id, send_async=False)

    assert db.session.query(UserFollowRequest).count() == 1
    follow = _sent_activity(route)
    assert follow['type'] == 'Follow'
    assert follow['object'] == target.public_url()
```

- [ ] **Step 3: Run** — `./run_tests.sh tests/test_shared_tasks_follows.py -q`, expect `2 passed`.

If either fails, read the failure before changing anything. `follow_user`'s signature is `(to_follow_id, user_id, send_async=True)` — the target comes FIRST, unlike every other task in this module. Confirm the argument order against `:214` rather than assuming.

- [ ] **Step 4: Commit** — subject: `test: open tests/test_shared_tasks_follows.py with the follow harness`

---

## Task 2: `join_community`'s happy paths and its instant-join arm

**Files:**
- Modify: `tests/test_shared_tasks_follows.py`

**Interfaces:**
- Consumes: everything from Task 1.

**Context:** `:74` is `if not community.is_local() and community.instance.online():`. Both conjuncts false-arm into the same place — `:91-93`'s cache invalidation, then the `src` fork at `:95-103`. A local community and an offline remote community both join instantly with no request sent.

- [ ] **Step 1: Write the three tests**

```python
def test_joining_a_local_community_sends_nothing(db_session, http_mock):
    """`:74`'s first conjunct. A local community joins instantly: no
    `CommunityJoinRequest` row, no Follow.

    The oracle is the `ActivityPubLog` count, not the absence of a route:
    `signature.py:143` swallows respx's unmatched-request assertion into a
    failure row, so a send here would be invisible to a call count.
    """
    s = _seed(with_keys=True)

    result = join_community(None, s.user.id, s.community.id, SRC_API)

    assert db.session.query(CommunityJoinRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0
    assert result is True


def test_joining_an_offline_remote_community_sends_nothing(db_session, http_mock):
    """`:74`'s second conjunct, `community.instance.online()`. A remote
    community on a dormant instance also joins instantly.

    Separated from the local test because the two conjuncts fail
    independently, and coverage.py records one arc pair for the whole `if`.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_online(s, online=False)

    result = join_community(None, s.user.id, s.community.id, SRC_API)

    assert db.session.query(CommunityJoinRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0
    assert result is True


def test_joining_returns_the_preload_status_for_src_pld(db_session, http_mock):
    """`:100-102`'s `SRC_PLD` arm. The dict is the only thing distinguishing
    this arm from `SRC_API`'s `return True` at `:104`, and under
    `task_always_eager` the wrapper hands the value back directly (fact 146).
    """
    s = _seed(with_keys=True)

    result = join_community(None, s.user.id, s.community.id, SRC_PLD)

    assert result == {'status': 'joined'}
```

- [ ] **Step 2: Run** — expect `5 passed`.

- [ ] **Step 3: Commit** — subject: `test: cover join_community's instant-join arms and its preload return`

---

## Task 3: `join_community`'s `SRC_WEB` arm, and the request-context interaction

**Files:**
- Modify: `tests/test_shared_tasks_follows.py`

**Context:** `:95-99` is the `SRC_WEB` arm and it calls `flash()`, which raises outside a request context. The `app` fixture pushes only an app context (`tests/conftest.py:112`).

- [ ] **Step 1: Write the test**

```python
def test_joining_flashes_and_returns_none_for_src_web(db_session, http_mock):
    """`:96-98`'s `flash`, guarded by `:95`'s `src == SRC_WEB`.

    TWO THINGS ABOUT THIS TEST ARE LOAD-BEARING AND NEITHER IS OBVIOUS.

    First, `flash()` requires a request context and `tests/conftest.py:112`
    pushes only an app context, so the test pushes its own. Without it the
    task raises `RuntimeError: Working outside of request context` and the
    failure looks like a bug in the task rather than in the test.

    Second, pushing that context DISABLES `patch_db_session`:
    `app/utils.py:3685` is `if has_request_context(): yield; return`. So
    `db.session` is NOT the task's session here, and every assertion below
    goes through a fresh query for that reason. An attribute read on an object
    this test built earlier would see the test's own session, not the task's.
    """
    s = _seed(with_keys=True)

    with current_app.test_request_context('/'):
        result = join_community(None, s.user.id, s.community.id, SRC_WEB)

    assert result is None
    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run and READ THE RESULT.** Expect `6 passed`. If instead you get `RuntimeError: Working outside of request context`, the request context is not reaching `flash()` — report that rather than removing the assertion.

- [ ] **Step 3: Commit** — subject: `test: cover join_community's SRC_WEB arm inside a request context`

---

## Task 4: `join_community`'s two rejection guards, all six arms

**Files:**
- Modify: `tests/test_shared_tasks_follows.py`

**Context:** `:44-56` rejects a banned user; `:60-72` rejects a community on a blocked or banned instance. Each has a three-way `src` fork inside `if not send_async:`, and each falls through to a bare `return` when `send_async` is truthy.

- [ ] **Step 1: Write the banned-user tests**

```python
def test_a_banned_user_cannot_join_and_raises_for_src_api(db_session, http_mock):
    """`:55`'s raise, guarded by `:54`'s `src == SRC_API`, inside `:45`'s
    banned check.

    `pytest.raises` matches on the message because all three `SRC_API` raises
    in this module are bare `Exception`; matching the type alone would not
    distinguish this guard from `:71`'s.
    """
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    with pytest.raises(Exception, match='banned_from_community'):
        join_community(None, s.user.id, s.community.id, SRC_API)

    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_banned_user_gets_the_preload_flag_for_src_pld(db_session, http_mock):
    """`:52`'s `pre_load_message['user_banned'] = True`, guarded by `:51`."""
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    result = join_community(None, s.user.id, s.community.id, SRC_PLD)

    assert result == {'user_banned': True}


def test_a_banned_user_joining_async_returns_without_a_message(db_session, http_mock):
    """`:56`'s bare `return`, reached when `send_async` is truthy so `:46`'s
    `if not send_async:` is False and the whole `src` fork is skipped.

    This is the arm an async caller takes, and it returns None rather than a
    message, which is why the preload and API callers pass `send_async` False.
    """
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    result = join_community(True, s.user.id, s.community.id, SRC_PLD)

    assert result is None
    assert db.session.query(CommunityJoinRequest).count() == 0
```

- [ ] **Step 2: Write the blocked-instance tests**

```python
def test_a_community_on_a_user_blocked_instance_cannot_be_joined(db_session, http_mock):
    """`:61`'s `user.has_blocked_instance(...)` disjunct, inside `:60`'s
    `not community.is_local()` conjunct. Per-user `InstanceBlock`, not a
    site-wide ban -- `:62`'s `instance_banned` reads a different table.
    """
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    result = join_community(None, s.user.id, s.community.id, SRC_PLD)

    assert result == {'community_on_banned_or_blocked_instance': True}
    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_community_on_a_site_banned_instance_cannot_be_joined(db_session, http_mock):
    """`:62`'s `instance_banned(...)` disjunct. Site-wide `BannedInstances`
    keyed by domain, which is a different table and a different scope from
    `:61`'s per-user block."""
    s = _seed(local_community=False, with_keys=True)
    _make_online(s)
    make_banned_instance('peer.example')
    db.session.commit()

    result = join_community(None, s.user.id, s.community.id, SRC_PLD)

    assert result == {'community_on_banned_or_blocked_instance': True}


def test_a_blocked_instance_raises_for_src_api(db_session, http_mock):
    """`:71`'s raise, guarded by `:70`. Distinguished from `:55`'s by message."""
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    with pytest.raises(Exception, match='community_on_banned_or_blocked_instance'):
        join_community(None, s.user.id, s.community.id, SRC_API)
```

Add `make_banned_instance`, `make_community_ban` and `make_instance_block` to the `tests.factories` import list.

- [ ] **Step 3: Run** — expect `12 passed`.

- [ ] **Step 4: Write the two `SRC_WEB` rejection tests**

```python
def test_a_banned_user_gets_a_flash_for_src_web(db_session, http_mock):
    """`:49`'s `return`, guarded by `:47`. The flash at `:48` needs a request
    context, and pushing one disables `patch_db_session` -- see
    `test_joining_flashes_and_returns_none_for_src_web` for why that matters.
    """
    s = _seed(with_keys=True)
    make_community_ban(s.user, s.community)

    with current_app.test_request_context('/'):
        result = join_community(None, s.user.id, s.community.id, SRC_WEB)

    assert result is None
    assert db.session.query(CommunityJoinRequest).count() == 0


def test_a_blocked_instance_gets_a_flash_for_src_web(db_session, http_mock):
    """`:66`'s `return`, guarded by `:64`."""
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    db.session.commit()

    with current_app.test_request_context('/'):
        result = join_community(None, s.user.id, s.community.id, SRC_WEB)

    assert result is None
```

- [ ] **Step 5: Run** — expect `14 passed`.

- [ ] **Step 6: Commit** — subject: `test: cover join_community's banned and blocked-instance guards`

---

## Task 5: PC1 — observe `leave_community`'s deleted-row read, then fix it

**Files:**
- Modify: `app/shared/tasks/follows.py:125-134`
- Modify: `tests/test_shared_tasks_follows.py`

**Context:** `:125` reads the join request, `:126` deletes it, `:127` commits, and `:134` then reads `join_request.uuid` to build the Follow id. `app/__init__.py:81` never overrides `expire_on_commit`, so the default `True` applies and the deleted instance's attributes are expired by `:127`.

**THIS TASK OBSERVES BEFORE IT FIXES.** The spec predicts `ObjectDeletedError`. That is a reading, not a measurement.

- [ ] **Step 1: Write the test that should expose it**

```python
def test_leaving_a_remote_community_sends_an_undo(db_session, http_mock):
    """`:134-153`. Leaving a remote community deletes the join request and
    sends an Undo wrapping the original Follow.

    THE FOLLOW ID INSIDE THE UNDO IS THE POINT. `:134` builds it from
    `join_request.uuid` -- the uuid of the row `:126` just deleted -- so the
    remote end can match the Undo to the Follow it originally received. An
    Undo carrying a fresh or missing id is not an Undo of anything.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_online(s)
    request = make_community_join_request(s.user, s.community)
    expected_uuid = request.uuid
    db.session.commit()
    route = _peer_route(http_mock)

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(CommunityJoinRequest).count() == 0
    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Follow'
    assert undo['object']['id'].endswith(expected_uuid)
```

- [ ] **Step 2: Run it and PASTE WHAT HAPPENS.** This is the task's evidence step.

The spec expects `sqlalchemy.orm.exc.ObjectDeletedError` raised out of the wrapper by `:154-155`'s `except Exception: session.rollback(); raise`. Paste the actual output — the final traceback line at minimum.

**If you get something else, stop and report it before changing any production code.** Possibilities the spec has not ruled out: the attribute survives because it was loaded before the delete and the identity map serves it; the test's own `db.session` interacts differently under `patch_db_session`; or `.first()` returns `None` and `:126` raises `UnmappedInstanceError` first. Each of those means PC1 changes shape, and the register entry has to describe what was found rather than what was expected.

- [ ] **Step 3: Make the production change**

Capture the uuid before the delete, matching `leave_feed:175-177`'s shape:

```python
            join_request = session.query(CommunityJoinRequest).filter_by(user_id=user_id, community_id=community_id).first()
            join_request_uuid = join_request.uuid
            session.delete(join_request)
            session.commit()
```

and change what was `:134` to build from the local:

```python
            follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request_uuid}"
```

**Do NOT add a `None` guard at `:125`.** `.first()` can return `None` and `session.delete(None)` raises; that stays as it is and gets registered at Task 12. PC1 is strictly the ordering fix so it makes exactly one claim.

- [ ] **Step 4: Run** — expect `15 passed`.

- [ ] **Step 5: Verify** — BEFORE committing, paste `git diff --numstat` (expect `2	1	app/shared/tasks/follows.py`) and `wc -l app/shared/tasks/follows.py` (expect 278 — the capture adds one line).

- [ ] **Step 6: Commit** — subject: `fix: read the join request's uuid before deleting it in leave_community`

---

## Task 6: PC2 — the same fix in `unfollow_user`

**Files:**
- Modify: `app/shared/tasks/follows.py:251-253`
- Modify: `tests/test_shared_tasks_follows.py`

**Context:** `:250`'s `if join_request:` already guards the None case here, so only the ordering is wrong: `:251` deletes, `:252` commits, `:253` reads `.uuid`.

- [ ] **Step 1: Write the test**

```python
def test_unfollowing_sends_an_undo_carrying_the_original_follow_id(
        db_session, http_mock):
    """`:253`'s read of the deleted row's uuid, and `:271`'s Undo.

    Same defect and same fix as `leave_community`. The `if join_request:`
    guard at `:250` already exists here, so this site's only fault is the
    ordering.
    """
    s = _seed(with_keys=True)
    peer = make_instance('peer.example', software='lemmy')
    target = make_user(peer, 'target')
    target.ap_inbox_url = PEER_INBOX
    db.session.commit()
    request = make_user_follow_request(s.user, target)
    expected_uuid = request.uuid
    db.session.commit()
    route = _peer_route(http_mock)

    unfollow_user(target.id, s.user.id, send_async=False)

    assert db.session.query(UserFollowRequest).count() == 0
    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['id'].endswith(expected_uuid)
```

- [ ] **Step 2: Run and PASTE THE FAILURE.** Expect the same exception Task 5 observed. If it differs from Task 5's, say so — two sites with the same shape failing differently is itself a finding.

- [ ] **Step 3: Make the production change**

```python
            if join_request:
                join_request_uuid = join_request.uuid
                session.delete(join_request)
                session.commit()
                to_follow_ap_id = f"{current_app.config['SERVER_URL']}/activities/follow_user/{join_request_uuid}"
```

- [ ] **Step 4: Run** — expect `16 passed`.

- [ ] **Step 5: Verify** — paste `git diff --numstat` and `wc -l` before committing.

- [ ] **Step 6: Commit** — subject: `fix: read the follow request's uuid before deleting it in unfollow_user`

---

## Task 7: `leave_community`'s and `unfollow_user`'s remaining arms

**Files:**
- Modify: `tests/test_shared_tasks_follows.py`

- [ ] **Step 1: Write the five tests**

```python
def test_leaving_a_local_community_sends_nothing(db_session, http_mock):
    """`:122`'s return, guarded by `:121`'s `community.is_local()`. Reached
    after the cache invalidation at `:118-119`, which runs for every caller.
    """
    s = _seed(with_keys=True)
    make_community_join_request(s.user, s.community)
    db.session.commit()

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0
    assert db.session.query(CommunityJoinRequest).count() == 1


def test_leaving_an_offline_community_deletes_the_request_but_sends_nothing(
        db_session, http_mock):
    """`:132`'s return, guarded by `:129`'s `not community.instance.online()`.

    THE ROW IS STILL DELETED. `:126-127` runs before the guard, so leaving an
    offline instance removes the local record and simply does not tell the
    remote end. Asserting only "nothing was sent" would miss that.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_online(s, online=False)
    make_community_join_request(s.user, s.community)
    db.session.commit()

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(CommunityJoinRequest).count() == 0
    assert db.session.query(ActivityPubLog).count() == 0


def test_leaving_a_community_on_a_blocked_instance_sends_nothing(
        db_session, http_mock):
    """`:130`'s `user.has_blocked_instance(...)` disjunct."""
    s = _seed(local_community=False, with_keys=True)
    peer = _make_online(s)
    make_instance_block(s.user, peer)
    make_community_join_request(s.user, s.community)
    db.session.commit()

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_leaving_a_community_on_a_banned_instance_sends_nothing(
        db_session, http_mock):
    """`:131`'s `instance_banned(...)` disjunct -- the site-wide table, not
    `:130`'s per-user one."""
    s = _seed(local_community=False, with_keys=True)
    _make_online(s)
    make_banned_instance('peer.example')
    make_community_join_request(s.user, s.community)
    db.session.commit()

    leave_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_unfollowing_with_no_request_row_sends_a_gibberish_follow_id(
        db_session, http_mock):
    """`:255`'s else arm, taken when `:250`'s `if join_request:` is False.

    The Undo still goes out, carrying a fabricated Follow id. Whether a remote
    end can match it is not this test's claim -- the claim is that the arm
    exists and sends.
    """
    s = _seed(with_keys=True)
    peer = make_instance('peer.example', software='lemmy')
    target = make_user(peer, 'target')
    target.ap_inbox_url = PEER_INBOX
    db.session.commit()
    route = _peer_route(http_mock)

    unfollow_user(target.id, s.user.id, send_async=False)

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Follow'
```

- [ ] **Step 2: Run** — expect `21 passed`.

- [ ] **Step 3: Commit** — subject: `test: cover leave_community's guards and unfollow_user's missing-request arm`

---

## Task 8: PC3 — `leave_feed`, and its swallowed exception

**Files:**
- Modify: `app/shared/tasks/follows.py:207-208`
- Modify: `tests/test_shared_tasks_follows.py`

**Context:** `:207-208` is `except Exception:` / `session.rollback()` with no `raise`. Every other task in this module re-raises. So a failure inside `leave_feed` is swallowed and the caller sees success.

- [ ] **Step 1: Write the happy path and the guard tests**

```python
def test_leaving_a_remote_feed_sends_an_undo(db_session, http_mock):
    """`:186-205`. `leave_feed` captures the uuid at `:177` BEFORE deleting at
    `:178`, which is the ordering `leave_community` and `unfollow_user` lacked
    until this sub-project fixed them. The correct idiom was always in this
    file, one function below the first defect.
    """
    s = _seed(with_keys=True)
    peer = make_instance('peer.example', software='lemmy')
    peer.inbox = PEER_INBOX
    feed = make_feed(peer, 'peerfeed')
    feed.ap_inbox_url = PEER_INBOX
    db.session.commit()
    request = make_feed_join_request(s.user, feed)
    expected_uuid = request.uuid
    db.session.commit()
    route = _peer_route(http_mock)

    leave_feed(None, s.user.id, feed.id)

    assert db.session.query(FeedJoinRequest).count() == 0
    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['id'].endswith(expected_uuid)


def test_leaving_a_local_feed_sends_nothing(db_session, http_mock):
    """`:173`'s return, guarded by `:172`'s `feed.is_local()`, after the three
    cache invalidations at `:168-170`."""
    s = _seed(with_keys=True)
    feed = make_local_feed('localfeed')
    db.session.commit()

    leave_feed(None, s.user.id, feed.id)

    assert db.session.query(ActivityPubLog).count() == 0
```

Add `FeedJoinRequest` to the `app.models` import list and `make_feed_join_request`, `make_local_feed` to the factories import.

- [ ] **Step 2: Write the test that pins the missing `raise`**

```python
def test_leave_feed_reraises_rather_than_swallowing(db_session, http_mock):
    """`:209`'s `raise`, added by this sub-project.

    Before this commit `:207-208` was `except Exception:` / `rollback()` with
    no re-raise, alone among the five tasks in this module. A failure inside
    `leave_feed` was swallowed: the caller saw success, Celery recorded
    nothing, and the user's feed membership silently failed to leave.

    A feed id that does not exist makes `:165`'s `.one()` raise
    `NoResultFound`, which is the cheapest way to reach the handler.
    """
    s = _seed(with_keys=True)

    with pytest.raises(Exception):
        leave_feed(None, s.user.id, 999999)
```

- [ ] **Step 3: Run and READ THE FAILURE.** Expect `test_leave_feed_reraises_rather_than_swallowing` to fail — `DID NOT RAISE`. Paste it.

- [ ] **Step 4: Make the production change**

```python
    except Exception:
        session.rollback()
        raise
```

- [ ] **Step 5: Run** — expect `24 passed`.

- [ ] **Step 6: Verify** — paste `git diff --numstat` (expect `1	0` for `follows.py` on this change alone) and `wc -l` (expect 280 — PC1, PC2 and this change each added one line to 277).

- [ ] **Step 7: Commit** — subject: `fix: stop leave_feed swallowing every failure it can produce`

---

## Task 9: Close `follows.py`, mutate, set the floor

**Files:**
- Modify: `tests/test_shared_tasks_follows.py`
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Measure**

```
./run_tests.sh tests/test_shared_tasks_follows.py -q --cov=app.shared.tasks.follows --cov-branch --cov-report=json --cov-report=term-missing
podman cp pyfedi_test-runner_1:/app/coverage.json ./coverage-follows.json
```

Use the DOTTED form. Report statements, branches, partials and the exact uncovered lines.

- [ ] **Step 2: Close the residual.** Write a test per uncovered line or arc. For anything you believe unreachable, give a per-line PROOF, not an assertion. Do not lower the floor to accommodate an unproven claim, and do not weaken a test to move a number.

- [ ] **Step 3: Mutate**

| # | Target | Mutation | Expected |
|---|--------|----------|----------|
| M1 | `:74` | drop `not community.is_local() and ` | killed by `test_joining_a_local_community_sends_nothing` |
| M2 | `:74` | drop ` and community.instance.online()` | killed by `test_joining_an_offline_remote_community_sends_nothing` |
| M3 | `:45` | `if banned:` → `if False:` | killed by `test_a_banned_user_cannot_join_and_raises_for_src_api` |
| M4 | `:61` | drop the `has_blocked_instance` disjunct | killed by `test_a_community_on_a_user_blocked_instance_cannot_be_joined` |
| M5 | `:62` | drop the `instance_banned` disjunct | killed by `test_a_community_on_a_site_banned_instance_cannot_be_joined` |
| M6 | PC1's capture | move the uuid read back after `session.commit()` | killed by `test_leaving_a_remote_community_sends_an_undo` |
| M7 | PC2's capture | same, in `unfollow_user` | killed by `test_unfollowing_sends_an_undo_carrying_the_original_follow_id` |
| M8 | PC3's `raise` | delete it | killed by `test_leave_feed_reraises_rather_than_swallowing` |
| M9 | `:121` | `if community.is_local():` → `if False:` | killed by `test_leaving_a_local_community_sends_nothing` |
| M10 | `:129` | drop `not community.instance.online() or ` | killed by `test_leaving_an_offline_community_deletes_the_request_but_sends_nothing` |
| M11 | `:250` | `if join_request:` → `if False:` | killed by `test_unfollowing_sends_an_undo_carrying_the_original_follow_id` |
| M12 | `:172` | `if feed.is_local():` → `if False:` | killed by `test_leaving_a_local_feed_sends_nothing` |

Re-derive every line number against the post-PC tree before running anything — PC1 and PC3 each add a line, so everything below them has moved. M6 and M7 are the mutations that matter most: they re-create the exact defect this sub-project fixed, and if either survives, the fix is not pinned.

One mutation at a time; dry-run without `-i` and paste the produced line; apply; run unpiped in the foreground; paste the real result; restore with `git checkout -- app/` and paste both `git diff --stat -- app/` (empty) and `wc -l`.

- [ ] **Step 4: Add the floor**

`coverage_floors.ini`, after `app/shared/tasks/deletes.py = 100`:

```
app/shared/tasks/follows.py = 100
```

- [ ] **Step 5: Prove the floor bites.** An isolated `--cov=app.shared.tasks.follows` run names ONE module, so every other floored entry reads 0.0 through `tests/check_coverage_floors.py:69`'s `files.get(module)` default and appears to violate. Build a SYNTHETIC multi-module report: merge the real `follows.py` entry into a report carrying passing entries for the other floored modules. State exactly how you built it. Invert the floor, show `check_coverage_floors.py` failing for `app/shared/tasks/follows.py` ALONE, restore, show it passing.

- [ ] **Step 6: Commit** — subject: `test: close app/shared/tasks/follows.py and set its floor`

---

## Task 10: The `notes.py` residual

**Files:**
- Modify: `tests/test_shared_tasks_send_reply.py`
- Modify: `coverage_floors.ini`

**Context:** `notes.py:98-101` is a bare `except: pass` around `search_for_user(user_name)` inside `send_reply` (`:80`). `search_for_user` is imported at module level (`notes.py:7`), so the patch target is `app.shared.tasks.notes.search_for_user`.

- [ ] **Step 1: Read the file first.** `tests/test_shared_tasks_send_reply.py` has its own harness and conventions. Match them — do not import this plan's `_seed` or build a second one. Find the existing test that exercises mention parsing and put the new one beside it.

- [ ] **Step 2: Write the test**

The file's harness gives you `_seed(body=..., with_parent_reply=False, local_community=True, with_keys=False)` and `_send(s)`. `:98-101` sits in `:95`'s TRUE arm — the LOCAL-mention branch — so the mention must be `@name@test.piefed.local`, matching `test_a_local_mention_resolves_and_is_notified` at `:404`.

```python
def test_a_failing_local_mention_lookup_is_swallowed(db_session, monkeypatch):
    """`notes.py:101`'s `pass`, guarded by `:100`'s bare `except:`.

    `search_for_user` is reached at `:99` for a LOCAL mention -- `:95`'s true
    arm -- and it can raise. The bare except swallows that and leaves
    `recipient` unbound for this iteration, so `:108`'s truthiness test is
    never reached for this mention and no Notification is written.

    THE PAIR IS THE POINT. `test_a_local_mention_resolves_and_is_notified`
    (`:404`) runs the same seed with the same body and a working lookup and
    gets exactly one Notification. This test differs in one variable and gets
    zero, so the difference isolates the swallow. Asserting only that the
    lines executed would not distinguish a swallow from a re-raise.
    """
    def _raising_search(*args, **kwargs):
        raise Exception('lookup failed')

    monkeypatch.setattr('app.shared.tasks.notes.search_for_user', _raising_search)
    s = _seed(body='hello @mentioned@test.piefed.local')
    make_user(s.instance, 'mentioned', local=True)

    _send(s)

    assert Notification.query.count() == 0
```

Re-derive `:404` and `:95`/`:99`/`:100`/`:101` against the current tree before shipping the docstring — this plan's numbers were read at authoring time and the file may have moved.

- [ ] **Step 3: Run** the file and confirm the new test passes and the count rose by one.

- [ ] **Step 4: Measure** `--cov=app.shared.tasks.notes` and confirm 100%.

- [ ] **Step 5: Raise the floor** — `app/shared/tasks/notes.py = 99` becomes `= 100`.

- [ ] **Step 6: Commit** — subject: `test: close notes.py's swallowed mention lookup and raise its floor`

---

## Task 11: The `pages.py` residual

**Files:**
- Modify: `tests/test_shared_tasks_send_post.py`
- Modify: `coverage_floors.ini`

**Context:** Four sites, all inside `send_post` (`pages.py:88`): the bare `except: pass` at `:107-108`, and three untaken branch arms — the false arms of `:270` and `:333`'s `if not community.local_only:`, and `:312`'s `if 'name' in page:`.

- [ ] **Step 1: Read the file first** and match its conventions, as in Task 10.

- [ ] **Step 2: Write the four tests**

**The mention test** mirrors Task 10's, with the patch target `app.shared.tasks.pages.search_for_user` (`pages.py:9` imports it at module level) and the file's own `_seed`/send helpers.

**`:270` and `:333` are the SAME condition — `if not community.local_only:` — and ONE test takes both false arms.** I read both sites: `:270` guards the community fan-out at `:271-309`, and `:333` guards the mention-recipient fan-out at `:334-337`. A `local_only` community skips both. So write one test, name both lines in its docstring, and assert the `ActivityPubLog` count is 0 — a `local_only` post sends nothing at all through either block. Do not write two tests asserting the same thing; do check, before you commit, that no OTHER statement between `:270` and `:337` runs unconditionally and would make the count non-zero.

**`:312`'s false arm needs a `page` with no `'name'` key.** `:312-313` deletes the title when converting a Page to a Note. Read whatever builds `page` (`post_to_page` or its equivalent — find it rather than assuming the name) and determine which post shape omits `name`. If every reachable post shape carries one, the arm is unreachable and the right deliverable is a PROOF of that in the report, not a contrived dict — say so and leave `pages.py` short of 100 with the reason recorded, rather than lowering the floor or faking the input.

Derive each assertion from what the arm actually skips. **Do not assert merely that the line was reached** — a branch test whose only effect is to move a coverage number is the artifact this campaign spends its reviews removing.

- [ ] **Step 3: Run** the file and confirm the count rose by two or three, depending on whether `:312`'s arm proved reachable. State which.

- [ ] **Step 4: Measure** `--cov=app.shared.tasks.pages` and confirm 100% with 0 partial branches.

- [ ] **Step 5: Raise the floor** — `app/shared/tasks/pages.py = 98` becomes `= 100`.

- [ ] **Step 6: Commit** — subject: `test: close pages.py's residual branches and raise its floor`

---

## Task 12: Register the findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

**Context:** Next free number is **D337** — confirm against the register. Facts end at **154**; append from **155**.

- [ ] **Step 1: Register the three fixed defects**

- **The uuid read after delete, at two sites**, fixed by PC1 and PC2. Record what Task 5 OBSERVED, not what the spec predicted — if they differ, the observation wins and the entry says so. Record that `leave_feed:175-177` had the correct idiom all along, one function below the first defect, which is D332's shape.
- **`leave_feed`'s swallowed exception**, fixed by PC3. Record what it made invisible: the caller saw success, Celery recorded nothing, and a feed membership silently failed to leave.

- [ ] **Step 2: Register the three findings not fixed**

- **`leave_community:125`'s unguarded `.first()`** — out of PC1's scope by design, with the reason.
- **`leave_feed`'s possibly-unbound `uuid`** — `:176` guards the assignment, nothing guards the use. If the coverage work proved the path unreachable, record the proof instead of the finding.
- **`follow_user` and `unfollow_user` never call `patch_db_session`** — D314's shape. Say whether it matters: if those functions touch nothing that reads `db.session`, the omission is inert and the entry should say so rather than implying a latent bug.

- [ ] **Step 3: Append the harness facts from 155**

- **`flash()` needs a request context and the `app` fixture does not push one.** `tests/conftest.py:112` pushes only `application.app_context()`. A test reaching a `flash()` call must push its own with `current_app.test_request_context('/')`.
- **Pushing a request context DISABLES `patch_db_session`.** `app/utils.py:3685` is `if has_request_context(): yield; return`. So the two facts above compose: a test that needs `flash()` cannot also rely on `db.session` being the task's session, and must assert through fresh queries. Verify both line numbers before writing them.
- **Reading an attribute off a deleted instance after `commit()`** — whatever Task 5 observed. Write this one from the measurement.

- [ ] **Step 4: Verify every citation** — open each line and confirm it carries the claimed statement. Re-derive citations into files your own diff touches after the diff is final.

- [ ] **Step 5: Commit** — subject: `docs: register sub-project 28's findings and harness facts`

---

## Final verification (controller only)

- [ ] Full suite, foreground and unpiped, on a stack reset with `./run_tests.sh --down` first.
- [ ] Collected counts from pytest's own output for all three changed test files.
- [ ] `git diff --numstat <base>..HEAD -- app/` — expect `5 2` for `follows.py` (PC1 two added one removed, PC2 two added one removed, PC3 one added, none removed) and no other production file. Re-derive this figure rather than trusting it.
- [ ] `wc -l app/shared/tasks/follows.py` — 280.
- [ ] Floors: 19 entries, all met, against a report whose mtime postdates the run.
- [ ] `git status --short` — clean.

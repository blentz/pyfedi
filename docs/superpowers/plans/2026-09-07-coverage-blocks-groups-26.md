# Sub-project 26: `task_selector`, `groups.py` and `blocks.py` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close `app/shared/tasks/__init__.py` (20 stmts, 83.3333%),
`app/shared/tasks/groups.py` (54 stmts, 12.8205%) and
`app/shared/tasks/blocks.py` (104 stmts, 11.9403%) to zero missing statements
and zero partial branches, landing three production changes.

**Architecture:** Three phases in ascending difficulty, each ending with a
floor committed before the next begins. Phase A (Task 1) closes `task_selector`
with a stub-substitution technique the campaign has not used before. Phase B
(Tasks 2-5) closes `groups.py`, whose 24 branches in 54 statements is the
densest ratio met so far. Phase C (Tasks 6-10) closes `blocks.py`, which forks
structurally on `community_id` and has three delivery paths. Task 11 registers.

**Tech Stack:** pytest, respx, SQLAlchemy 2.0.52, Flask, Celery.

**Spec:** `docs/superpowers/specs/2026-09-07-coverage-blocks-groups-26-design.md`

**Note on the production changes.** The spec numbers them 1 (`blocks.py:104`),
2 (`groups.py:59`) and 3 (`blocks.py:189`), but this plan executes `groups.py`
before `blocks.py`, so they land in the order 2, 1, 3. Tasks below name the
SITE rather than the number to avoid the confusion that ordering would cause.

## Global Constraints

- **Delete nothing you did not create.**
- Only the controller runs the full suite, one pytest session at a time, **in the foreground**.
- `pytest` exits 1 on a session timeout and `run_tests.sh` propagates it. A pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- Coverage takes the **dotted module form**: `--cov=app.shared.tasks.blocks`. A path form collects nothing and exits 0. The JSON lands **inside** the `pyfedi_test-runner` container; retrieve it with `podman cp` (fact 137).
- **Test counts come from collection**, never `grep -c '^def test_'`.
- Before believing any failure, run `./run_tests.sh --down`.
- **Mutations:** one at a time, targeted single-line `sed`, **dry-run without `-i` and read the produced line first**, apply, run, restore with `git checkout -- app/`, then assert an empty `git diff -- app/` and the expected `wc -l`. **Restore before any point where you might stop and report** (fact 145). Paste every dry-run line and every failure; a narrated mutation result is worth nothing.
- A surviving mutation is information — usually about the test, but sometimes about the code (fact 142). Check whether the mutated construct is genuinely redundant before treating a survival as a gap.
- Commit with `git commit -F <file>`, never `-m`. Normal English prose.
- **No ordered assertions over rows a query planner returned.** Set-based or sorted. Order imposed by straight-line Python is fine.
- Every line number re-derived against the current tree, **and citations into a file your own diff touches re-derived after the diff is final** (fact 139.3).
- Where prose describes a **statement**, cite that statement's line, not the `if` guarding it. Block-header citations are for blocks.
- When you change a line, read the prose **attached** to it, not only prose elsewhere citing it (fact 139.4).
- Per fact 132, decide class-level vs instance-level monkeypatching per call site by reading whether the production path re-loads the object; confirm with a raising probe.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_shared_tasks_dispatcher.py` | **Create.** All `task_selector` coverage. |
| `tests/test_shared_tasks_groups.py` | **Create.** All `groups.py` coverage. |
| `tests/test_shared_tasks_blocks.py` | **Create.** All `blocks.py` coverage. |
| `tests/factories.py` | **Modify.** Add `make_file`. |
| `app/shared/tasks/groups.py:59` | **Modify.** Production change 2 (D309). |
| `app/shared/tasks/blocks.py:104` | **Modify.** Production change 1 (D309). |
| `app/shared/tasks/blocks.py:189` | **Modify.** Production change 3 (fallback guard). |
| `coverage_floors.ini` | **Modify.** Three entries, added at three different times. |
| `tests/README.md` | **Modify.** Facts from 146. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify.** New numbers from D328, plus in-place edits. |

---

# PHASE A — `task_selector`

## Task 1: Close `app/shared/tasks/__init__.py`

**Files:**
- Create: `tests/test_shared_tasks_dispatcher.py`
- Modify: `coverage_floors.ini`

**Interfaces:**
- Produces: nothing later tasks consume. This phase stands alone.

**Context:** `task_selector` at `app/shared/tasks/__init__.py:61` is the
dispatcher every task in the package routes through. It is missing `:63` and
`:68`, with partial arcs `(62,63)` and `(65,68)`.

```
:62      if current_app.debug:
:63          send_async = False                                       <- MISSING
:65      if send_async:
:66          tasks[task_key].delay(send_async=send_async, **kwargs)
:67      else:
:68          return tasks[task_key](send_async=send_async, **kwargs)   <- MISSING
```

**THE TECHNIQUE THIS TASK INTRODUCES.** The `tasks` dict is built inside the
function from imports that also happen inside the function
(`app/shared/tasks/__init__.py:62` onward is preceded by `from
app.shared.tasks.flags import report_reply, report_post` and its siblings). Those
imports resolve **at call time**, so patching the module attribute
`app.shared.tasks.flags.report_post` before calling `task_selector` puts your
stub into the dict.

**THIS IS THE ONLY WAY TO DISCRIMINATE `:68` FROM `:66`.** Under
`task_always_eager` both arms execute synchronously, so "the task ran" is true
either way. The difference is the return: `:66` falls off the end and returns
`None`; `:68` returns whatever the task returned. A stub returning a sentinel
makes that observable.

Note the stub CANNOT be used for the `:66` arm — `:66` calls `.delay()`, which a
plain function does not have. `:66` is already covered; do not add a test for it.

- [ ] **Step 1: Write the file**

```python
"""`task_selector` -- the dispatcher every task in this package routes through.

`app/shared/tasks/__init__.py:61-68`. Twenty statements, and until this file
existed it sat at 83.3333% with two missing: `:63`'s debug override and `:68`'s
SYNCHRONOUS dispatch arm.

WHY THOSE TWO AND NOT THE OTHERS. `tests/conftest.py` sets `task_always_eager`,
so `:66`'s `.delay()` already runs synchronously in every test that goes
through this function -- which is exactly why `:66` was covered and `:68` was
not. And `current_app.debug` is False under test, so `:63` never ran.

THE TWO ARMS ARE INDISTINGUISHABLE BY SIDE EFFECT AND DISTINGUISHABLE ONLY BY
RETURN VALUE. Under eager Celery both `:66` and `:68` execute the task
synchronously, so "the task ran" is true under either. `:66` falls off the end
and returns None; `:68` returns the task's own return value. Every test here
asserts on the RETURN, because asserting the task ran would pass under both
arms -- the vacuous shape this campaign exists to catch.

HOW THE STUB REACHES THE DICT. `task_selector` builds `tasks` from imports
performed INSIDE the function body, so those names resolve at call time.
Patching `app.shared.tasks.flags.report_post` before the call therefore puts
the stub into the dict. The stub cannot be used to exercise `:66`, which calls
`.delay()` on a Celery task object a plain function does not have -- but `:66`
needs no test, having been covered incidentally all along.
"""

from types import SimpleNamespace

import pytest
from flask import current_app

from app.shared.tasks import task_selector

_SENTINEL = object()


def _stub_report_post(monkeypatch):
    """Replace `report_post` in ITS OWN MODULE with a recording stub.

    Returns `SimpleNamespace(calls=[])`; the stub appends its kwargs and
    returns `_SENTINEL`, which is what makes `:68`'s return observable.

    THE PATCH TARGET IS `app.shared.tasks.flags`, not `app.shared.tasks`.
    `task_selector` does `from app.shared.tasks.flags import report_post`
    inside its own body, so the name is looked up on the flags module when the
    call happens. Patching `app.shared.tasks.report_post` would not exist to
    patch, and patching the local `tasks` dict is impossible -- it is rebuilt
    on every call.
    """
    record = SimpleNamespace(calls=[])

    def _stub(**kwargs):
        record.calls.append(kwargs)
        return _SENTINEL

    monkeypatch.setattr('app.shared.tasks.flags.report_post', _stub)
    return record


def test_send_async_false_dispatches_synchronously_and_returns(
        db_session, monkeypatch):
    """`:68`, reached by `send_async=False`.

    THE RETURN IS THE ASSERTION. `:66` returns None; `:68` returns the task's
    value. Asserting only that the stub was called would pass under either arm,
    because `task_always_eager` makes `.delay()` synchronous too.
    """
    record = _stub_report_post(monkeypatch)

    result = task_selector('report_post', send_async=False, user_id=1,
                           post_id=2, summary='spam', instance_ids=[])

    assert result is _SENTINEL
    assert record.calls == [{'send_async': False, 'user_id': 1, 'post_id': 2,
                             'summary': 'spam', 'instance_ids': []}]


def test_debug_forces_synchronous_dispatch_despite_send_async_true(
        db_session, monkeypatch):
    """`:63`, and the arc `(62,63)`.

    THIS TEST IS ALSO A `:68` TEST, AND THAT IS THE POINT. `:63` sets
    `send_async = False`, so `:65` then takes the else arm. Passing
    `send_async=True` and still getting `_SENTINEL` back is what proves the
    override happened -- with debug False, the same call would take `:66` and
    return None.

    The stub records what it was called with, so the assertion also pins that
    the OVERRIDDEN value is what reaches the task, not the caller's `True`.
    """
    record = _stub_report_post(monkeypatch)
    monkeypatch.setitem(current_app.config, 'DEBUG', True)
    monkeypatch.setattr(current_app, 'debug', True, raising=False)

    result = task_selector('report_post', send_async=True, user_id=1,
                           post_id=2, summary='spam', instance_ids=[])

    assert result is _SENTINEL
    assert record.calls[0]['send_async'] is False


def test_an_unknown_task_key_raises_KeyError(db_session, monkeypatch):
    """`tasks[task_key]` with a key the dict does not carry.

    A dispatcher's failure mode for a typo'd key, asserted by a NATURAL raise
    rather than an injected one. This is the behaviour that makes a
    misspelled key loud at the call site instead of silently doing nothing.
    """
    _stub_report_post(monkeypatch)

    with pytest.raises(KeyError):
        task_selector('report_psot', send_async=False, user_id=1)
```

- [ ] **Step 2: Run**

Run: `./run_tests.sh tests/test_shared_tasks_dispatcher.py -v`
Expected: PASS, 3 tests.

**If `test_debug_forces...` fails**, the `current_app.debug` override is the
thing to check first. Flask derives `debug` from the `DEBUG` config key, but
`app.debug` is also a settable property; the test sets both because which one
`current_app.debug` reads depends on how the app was constructed. Read
`tests/conftest.py`'s app fixture and keep whichever override actually works,
deleting the other and saying so in your report.

- [ ] **Step 3: Measure**

```bash
./run_tests.sh tests/test_shared_tasks_dispatcher.py \
  --cov=app.shared.tasks --cov-branch \
  --cov-report=json:/tmp/dispatcher_cov.json
podman cp pyfedi_test-runner_1:/tmp/dispatcher_cov.json <scratchpad>/dispatcher_cov.json
```

Read `missing_lines` and `num_partial_branches` for
`app/shared/tasks/__init__.py`. Note `--cov=app.shared.tasks` covers the
package; the `__init__.py` entry is the one to read.

- [ ] **Step 4: Close any residual**, or document a proof if a line is
genuinely unreachable.

- [ ] **Step 5: Mutate**

| # | Target | Mutation | Expected |
|---|---|---|---|
| M1 | `:62` | `if current_app.debug:` → `if False:` | killed by `test_debug_forces_synchronous_dispatch_despite_send_async_true` |
| M2 | `:68` | delete the `return` keyword, leaving the call | killed by `test_send_async_false_dispatches_synchronously_and_returns` |

M2 is the one that matters: it makes `:68` behave like `:66` from the caller's
side while still executing the task. If it survives, the return assertion is
not doing its job.

Standard instrument: read the line, dry-run without `-i` and confirm the output
differs, apply, run, restore, then assert an empty `git diff -- app/` and the
file's original `wc -l`.

- [ ] **Step 6: Add the floor**

```ini
app/shared/tasks/__init__.py = 100
```

- [ ] **Step 7: Prove it bites** by inversion on a COPY of the report with an
isolation control; delete the copies.

- [ ] **Step 8: Commit**

Subject: `test: close app/shared/tasks/__init__.py's dispatcher`

- [ ] **Step 9: PHASE A CHECKPOINT.** Report the collected count, coverage
figures, both mutation results, and confirm the floor is committed. Phase B
does not begin until this is done.

---

# PHASE B — `groups.py`

## Task 2: Open `tests/test_shared_tasks_groups.py`

**Files:**
- Create: `tests/test_shared_tasks_groups.py`
- Modify: `tests/factories.py`

**Interfaces:**
- Produces: `make_file(file_path=None, source_url=None)` in `tests/factories.py`;
  `_seed(local_community=True, with_keys=False)` returning
  `SimpleNamespace(instance, user, community)`; `_make_deliverable(s, online=True)`;
  `_follower(s, http_mock, inbox, domain)` returning `(route, instance)`;
  `_sent_activity(route, index=-1)`; `_delivered_inboxes(*routes)`.
  Tasks 3-5 consume all of these.

**Context:** `groups.py` has one function, `edit_community` at `:52`. Copy the
harness from `tests/test_shared_tasks_locks.py`, which is the most recently
reviewed shape in this campaign — read it before writing.

**THE MEMBERSHIP REQUIREMENT CARRIES OVER.** `Community.following_instances()`
(`app/models.py:842-851`) joins `CommunityMember` and filters `Instance.id != 1`,
so a recipient needs an Instance row, an inbox, AND a user on that instance who
is a member of the community. Without it the loop never runs and delivery
assertions pass against zero deliveries (fact 141).

**`_seed` MUST MAKE ITS USER A MODERATOR.** `edit_community:62` returns unless
`community.is_moderator(user)`, and `Community.is_moderator`
(`app/models.py:736-740`) checks `moderator.user_id == user.id` over
`self.moderators()`. So the happy path needs
`make_community_member(user, community, is_moderator=True)`. Read
`make_community_member`'s signature before using it — re-derive its line number,
since a sibling task appends to that file.

- [ ] **Step 1: Add the `make_file` factory**

Append to `tests/factories.py`, adding `File` to the `app.models` import list if
absent — check first.

```python
def make_file(file_path: str = None, source_url: str = None) -> File:
    """A File row, for controlling what `Community.icon_image()` returns.

    `icon_image()` (app/models.py:659-670) prefers `file_path` over
    `source_url`, and rewrites either one that starts with `app/` into a
    `/`-rooted path. So a caller wanting an ABSOLUTE url passes
    `file_path='https://cdn.example/icon.png'`, and one wanting a RELATIVE path
    passes `file_path='/static/icon.png'` -- neither starts with `app/`, so
    both are returned unchanged and the difference is only the scheme.

    That distinction is what `groups.py:90` and `:101` branch on.
    """
    f = File(file_path=file_path, source_url=source_url)
    db.session.add(f)
    db.session.commit()
    return f
```

- [ ] **Step 2: Write the module docstring and imports**

```python
"""`edit_community` -- the AP Update sender for a local community's Group actor.

`app/shared/tasks/groups.py`, 149 lines, ONE function at `:52`. Fifty-four
statements and TWENTY-FOUR branches -- the densest ratio of any module this
campaign has closed. Most of that density is `:85-113`, where four optional
fields each add an arm and two of them branch AGAIN on whether the stored
image url is absolute.

TWO EARLY RETURNS, AND THE SECOND IS ONLY REACHABLE PAST THE FIRST. `:59` is
D309's site in this module; `:62` returns unless the acting user moderates the
community. So every test past the guards needs a moderator, which `_seed`
supplies.

DELIVERY REQUIRES COMMUNITY MEMBERSHIP, NOT MERELY AN INSTANCE ROW.
`following_instances()` (app/models.py:842-851) joins CommunityMember and
filters `Instance.id != 1`, so a recipient needs an Instance, an inbox, and a
member on it. Without the membership the loop at `:140` never runs and every
delivery assertion passes against zero deliveries.

ORDER IS NEVER ASSERTED. `following_instances()` ends in an unordered
`.distinct().all()`.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.groups import edit_community
from tests.factories import (
    make_community, make_community_member, make_file, make_instance, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'
```

- [ ] **Step 3: Write the helpers**

Transfer `_peer`, the autouse resolver fixture, `_make_deliverable`,
`_sent_activity` and `_delivered_inboxes` from
`tests/test_shared_tasks_locks.py` verbatim. Write `_seed` and `_follower`:

```python
def _seed(local_community=True, with_keys=False):
    """instance, user, community -- committed, with `user` a MODERATOR.

    ORDER IS LOAD-BEARING. `make_community` hardcodes `instance_id=1` and the
    db_session teardown resets every sequence, so the local instance is created
    FIRST; a peer built before this call would take id 1 and leave the
    community's FK pointing at it.

    THE MODERATOR MEMBERSHIP IS NOT OPTIONAL. `edit_community:62` returns
    unless `community.is_moderator(user)`, which checks
    `moderator.user_id == user.id` over `self.moderators()`
    (app/models.py:736-740). Without it every test past `:62` would assert
    against an early return.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795) is a DISJUNCTION whose
    `profile_id()` falls back to a computed default; `ap_id` alone leaves the
    community silently LOCAL (fact 112).
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'themod', local=True, with_keys=with_keys)
    community = make_community('c1')
    if with_keys:
        community.private_key = user.private_key
        community.public_key = user.public_key
    make_community_member(user, community, is_moderator=True)
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community)


def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example'):
    """A remote instance `following_instances()` will actually return.

    THREE parts, and the third is the one that is easy to miss: an Instance
    row, an inbox, and a USER ON THAT INSTANCE WHO IS A MEMBER OF THE
    COMMUNITY. Without the membership the query returns nothing, `:140`'s loop
    never runs, and every delivery assertion here passes against zero
    deliveries.

    Returns `(route, instance)`.
    """
    inst = make_instance(domain, software='lemmy')
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst
```

- [ ] **Step 4: Write the two smoke tests**

```python
def test_edit_community_announces_an_update_to_a_following_instance(
        db_session, http_mock):
    """`edit_community` end to end on a LOCAL community: both guards passed,
    the Group envelope at `:68-116`, the Update at `:120-128`, and the Announce
    at `:131-138` delivered at `:142`.

    The nested `@context` absence is the discriminating assertion.
    `app/activitypub/signature.py:100-101` reinjects at the TOP LEVEL only, so
    `'@context' in announce` would hold whether or not the code set it -- but
    the reinjection never reaches inside, so `:137`'s deletion of the Update's
    context is genuinely observable.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    edit_community(None, s.user.id, s.community.id)

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Update'
    assert announce['object']['object']['type'] == 'Group'
    assert announce['object']['object']['preferredUsername'] == s.community.name


def test_a_remote_community_receives_the_update_unwrapped(
        db_session, http_mock):
    """`:143-144`'s else arm: no Announce, the Update goes straight to
    `community.ap_inbox_url` signed with the USER's key rather than the
    community's.

    NO `@context` ASSERTION BELONGS ON THE UPDATE ITSELF. It is the top-level
    object here, so `signature.py:100-101` reinjects one whether or not the
    code set it. The nested Group's shape is what this test pins.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    edit_community(None, s.user.id, s.community.id)

    update = _sent_activity(route)
    assert update['type'] == 'Update'
    assert update['object']['type'] == 'Group'
```

- [ ] **Step 5: Run** — expect 2 tests.

- [ ] **Step 6: Commit** — subject: `test: open tests/test_shared_tasks_groups.py with the Group harness`

---

## Task 3: The two guards, and the production change at `groups.py:59`

**Files:**
- Modify: `app/shared/tasks/groups.py:59`
- Modify: `tests/test_shared_tasks_groups.py`

**Context:** `:59` is `if community.local_only: return` — omitting **both**
`private` and the `online()` check that every other D309 site already had.
`:62` is the moderator gate.

- [ ] **Step 1: Write the moderator test and the local_only test**

```python
def test_a_non_moderator_sends_nothing(db_session, http_mock):
    """`:62`'s true arm. Reached only past `:59`, so the community must be
    neither local_only nor (post-fix) private.

    NO ROUTE IS REGISTERED -- `http_mock` uses `assert_all_called=True`, so a
    route that never fires would fail this test for the wrong reason. The
    follower is built inline WITHOUT a route so the loop has a candidate,
    which is what makes the zero count mean "the guard returned" rather than
    "the query was empty".

    `post_request` writes its ActivityPubLog row at
    app/activitypub/signature.py:105 before any network attempt, so a count of
    0 distinguishes a guard return from a failed delivery.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    outsider = make_user(s.instance, 'outsider', local=True)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    edit_community(None, outsider.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_only_community_sends_nothing(db_session, http_mock):
    """`:59`'s first disjunct."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Write the two failing tests for the new conjuncts**

```python
def test_a_private_community_sends_nothing(db_session, http_mock):
    """D309's site in this module, FIRST of the two conjuncts this fix adds.

    Before this commit `:59` read `if community.local_only:` alone -- it
    omitted `private` AND the `instance.online()` check that every other member
    of this family already carried. That makes this a WIDER gap than the eight
    sites closed before it.

    `private` is placed BEFORE the `online()` call: `Community.instance_id` is
    a nullable FK and `or` short-circuits left to right, so a private community
    with no instance row returns at the guard rather than raising
    AttributeError. That is a side effect this test does not assert --
    reordering would reopen the crash without failing it.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_nothing(db_session, http_mock):
    """The SECOND conjunct this fix adds. `Instance.online()`
    (app/models.py:118-119) is exactly `not (self.dormant or
    self.gone_forever)`, so `_make_deliverable(s, online=False)` sets both.

    Separated from the private test because the two fail independently and one
    test could not say which conjunct returned.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 3: Run and READ BOTH FAILURES**

Expected: both fail on `assert 1 == 0`. **That row is a FAILED-delivery row,
not a completed send** — `post_request` writes it at `signature.py:105` before
any network attempt, catches the unregistered-route exception at `:143-148`,
marks it `failure`, and commits at `:151` regardless. Three implementers in
this campaign have described that row as a successful federation; do not be the
fourth. If either test fails for another reason, stop and fix that first.

- [ ] **Step 4: Make the production change**

`app/shared/tasks/groups.py:59`, from:

```python
                if community.local_only:
```

to:

```python
                if community.local_only or community.private or not community.instance.online():
```

- [ ] **Step 5: Run** — expect 6 tests.

- [ ] **Step 6: Verify** — `git diff --numstat -- app/shared/tasks/groups.py`
must be `1 1`; `wc -l app/shared/tasks/groups.py` must be 149.

- [ ] **Step 7: Commit** (tests + change in ONE commit)

Subject: `fix: stop edit_community federating a private or offline community`

---

## Task 4: The branch-dense envelope

**Files:**
- Modify: `tests/test_shared_tasks_groups.py`

**Context:** `:85-113` is where most of `groups.py`'s 24 branches live. Six
arms, and the two image ones nest:

```
:85   if community.description_html:
:87   if community.description:
:89   if community.icon_id:
:90       if community.icon_image().startswith('http'):
:95       else:
:100  if community.image_id:
:101      if community.header_image().startswith('http'):
:106      else:
```

`make_file(file_path=...)` controls what `icon_image()` returns.
`Community.icon_image` (`app/models.py:659-670`) returns `file_path` unchanged
unless it starts with `app/`, so `'https://cdn.example/icon.png'` gives the
absolute arm and `'/static/icon.png'` gives the relative one.

**WRITE ONE TEST PER ARM, NOT A PARAMETRISED SWEEP.** A parametrised failure
names the parameter rather than the branch, and an arm covered under only one
parameter is invisible in the failure output.

- [ ] **Step 1: Write the four optional-field tests**

```python
def test_a_description_html_becomes_the_summary(db_session, http_mock):
    """`:86`, guarded by `:85`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    s.community.description_html = '<p>hello</p>'
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert group['summary'] == '<p>hello</p>'


def test_no_description_html_omits_the_summary(db_session, http_mock):
    """`:85`'s false arm. The control for the test above: without it,
    `summary` being present is never distinguished from it being
    unconditional."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert 'summary' not in group


def test_a_description_becomes_the_markdown_source(db_session, http_mock):
    """`:88`, guarded by `:87`. `source` carries the raw markdown alongside
    the rendered `summary`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    s.community.description = 'hello'
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert group['source'] == {'content': 'hello', 'mediaType': 'text/markdown'}


def test_no_description_omits_the_source(db_session, http_mock):
    """`:87`'s false arm."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert 'source' not in group
```

- [ ] **Step 2: Write the four image tests**

```python
def test_an_absolute_icon_url_is_sent_unchanged(db_session, http_mock):
    """`:91-94`, the true arm of `:90`.

    `Community.icon_image()` (app/models.py:659-670) returns `file_path`
    unchanged when it does not start with `app/`, so an https path reaches
    `:90`'s `startswith('http')` as True and is used as-is.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    s.community.icon_id = make_file(file_path='https://cdn.example/icon.png').id
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert group['icon'] == {'type': 'Image',
                             'url': 'https://cdn.example/icon.png'}


def test_a_relative_icon_url_is_prefixed_with_the_server_url(
        db_session, http_mock):
    """`:96-99`, the false arm of `:90`. A path with no scheme is joined to
    SERVER_URL, which is what makes a locally-stored icon resolvable to a
    remote reader."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    s.community.icon_id = make_file(file_path='/static/icon.png').id
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert group['icon']['url'].endswith('/static/icon.png')
    assert group['icon']['url'].startswith('https://test.piefed.local')


def test_an_absolute_header_url_is_sent_unchanged(db_session, http_mock):
    """`:102-105`, the true arm of `:101`. Written out separately from the
    icon's because they are different branches on different columns -- a
    regression could drop either."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    s.community.image_id = make_file(file_path='https://cdn.example/hdr.png').id
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert group['image'] == {'type': 'Image',
                              'url': 'https://cdn.example/hdr.png'}


def test_a_relative_header_url_is_prefixed_with_the_server_url(
        db_session, http_mock):
    """`:107-110`, the false arm of `:101`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    s.community.image_id = make_file(file_path='/static/hdr.png').id
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert group['image']['url'].endswith('/static/hdr.png')
    assert group['image']['url'].startswith('https://test.piefed.local')


def test_no_icon_or_header_omits_both_keys(db_session, http_mock):
    """`:89`'s and `:100`'s false arms, together. They are separate branches
    but neither has any interaction with the other, so one control covers
    both -- and the two smoke tests already exercise this state incidentally,
    which is why this test asserts the ABSENCE explicitly rather than relying
    on that."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert 'icon' not in group
    assert 'image' not in group
```

- [ ] **Step 3: Run** — expect 15 tests.

- [ ] **Step 4: Commit** — subject: `test: cover the Group envelope's optional fields and image arms`

---

## Task 5: Close `groups.py`, mutate, set its floor

**Files:**
- Modify: `tests/test_shared_tasks_groups.py` (only if coverage shows gaps)
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Cover the language loop and the delivery guard**

```python
def test_a_community_with_no_languages_sends_an_empty_language_list(
        db_session, http_mock):
    """`:112`'s loop over zero iterations. `make_community` attaches no
    languages, so `group['language']` is built and left empty rather than
    omitted -- which is what distinguishes this from the optional fields at
    `:85-110`, where absence means the key is missing entirely."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert group['language'] == []


def test_each_community_language_becomes_an_identifier_and_name(
        db_session, http_mock):
    """`:113`, the loop body. Two languages so the arc back to the top of the
    loop is taken, which one language would not prove.

    SET-BASED, because `community.languages` is a relationship whose ordering
    this test does not control."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    en = Language(name='English', code='en')
    de = Language(name='German', code='de')
    db.session.add_all([en, de])
    db.session.commit()
    s.community.languages.append(en)
    s.community.languages.append(de)
    db.session.commit()

    edit_community(None, s.user.id, s.community.id)

    group = _sent_activity(route)['object']['object']
    assert {(l['identifier'], l['name']) for l in group['language']} == {
        ('en', 'English'), ('de', 'German')}


def test_a_follower_without_an_inbox_is_skipped_and_the_loop_continues(
        db_session, http_mock):
    """`:141`'s `instance.inbox` conjunct, with a second follower proving the
    loop CONTINUES rather than aborting.

    THE ActivityPubLog COUNT IS WHAT MAKES THIS DISCRIMINATE. An instance with
    a None inbox reaches `send_post_request(None, ...)`, and `post_request`
    takes the `if uri is None` arm at `app/activitypub/signature.py:109`,
    marking its already-written row `empty uri` at `:110-111` WITHOUT making an
    httpx call. So respx sees nothing and a delivered-inboxes assertion alone
    cannot tell a truthful skip from a mutated one. The row count can: one row
    for the real delivery, none for a correctly skipped instance.

    THE INBOXLESS FOLLOWER IS CREATED FIRST so it takes the lower id. This
    relies on Postgres returning a small unordered join in ascending id, which
    is an assumption about the query plan rather than a guarantee. If it ever
    breaks, this test degrades to LAX -- it would pass under an aborting loop
    too -- never to FLAKY: no direction exists in which a correct, continuing
    loop starts failing.
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

    edit_community(None, s.user.id, s.community.id)

    assert _delivered_inboxes(route) == {OTHER_INBOX}
    assert db.session.query(ActivityPubLog).count() == 1
```

Add `Language` to the `app.models` import at the top of the file, and
re-derive the import block after your edit is final.

- [ ] **Step 2: Measure**

```bash
./run_tests.sh tests/test_shared_tasks_groups.py \
  --cov=app.shared.tasks.groups --cov-branch \
  --cov-report=json:/tmp/groups_cov.json
podman cp pyfedi_test-runner_1:/tmp/groups_cov.json <scratchpad>/groups_cov.json
```

- [ ] **Step 3: Mutate**

| # | Target | Mutation | Expected |
|---|---|---|---|
| M3 | `:59` | drop `community.private or` | killed by `test_a_private_community_sends_nothing` |
| M4 | `:59` | drop `not community.instance.online()` | killed by `test_an_offline_community_instance_sends_nothing` |
| M5 | `:62` | `if not community.is_moderator(user):` → `if False:` | killed by `test_a_non_moderator_sends_nothing` |
| M6 | `:90` | `startswith('http')` → `startswith('zzz')` | killed by `test_an_absolute_icon_url_is_sent_unchanged` |
| M7 | `:101` | `startswith('http')` → `startswith('zzz')` | killed by `test_an_absolute_header_url_is_sent_unchanged` |
| M8 | `:141` | change `instance.inbox and` to `True and` | killed by the `:141` skip test from Step 1 |

M8 is the one that has survived before in this campaign when the skip test
asserted only on delivered inboxes. If it survives here, the Step 1 test needs
the `ActivityPubLog` count assertion, not a weaker one.

- [ ] **Step 4: Add the floor** — `app/shared/tasks/groups.py = 100`

- [ ] **Step 5: Prove it bites** by inversion with an isolation control.

- [ ] **Step 6: Commit** — subject: `test: close app/shared/tasks/groups.py and set its floor`

- [ ] **Step 7: PHASE B CHECKPOINT.** Phase C does not begin until this floor
is committed.

---

# PHASE C — `blocks.py`

## Task 6: Open `tests/test_shared_tasks_blocks.py`

**Files:**
- Create: `tests/test_shared_tasks_blocks.py`

**Interfaces:**
- Produces: `_seed(local_community=True, with_keys=False)` returning
  `SimpleNamespace(instance, user, mod, community)`; `_make_deliverable`;
  `_follower`; `_sent_activity`; `_delivered_inboxes`; `_site_instance(domain,
  software, inbox)`. Tasks 7-10 consume these.

**Context:** Four `@celery.task` wrappers delegating to `ban_person` at `:96`:

```
:41  ban_from_site(send_async, user_id, mod_id, expiry, reason, remove_data)
:46      ban_person(session, user_id, mod_id, None, expiry, reason, remove_data)
:55  unban_from_site(...)      :60  ban_person(..., None, ..., False, is_undo=True)
:69  ban_from_community(...)   :74  ban_person(..., community_id, ..., False)
:83  unban_from_community(...) :88  ban_person(..., community_id, ..., False, is_undo=True)
```

`_seed` needs BOTH a `user` (the person banned) and a `mod` (the person doing
it), because `ban_person` loads both at `:99-100` and signs with the mod's key.

**THE SITE-BAN PATH DOES NOT USE `following_instances()`.** `:159` runs
`session.query(Instance).filter(Instance.software != 'mastodon').all()` — every
instance except mastodon ones — and guards inline with `instance.inbox and
instance.online() and instance.id != 1`. So a site-ban recipient needs NO
community membership, only an Instance row with an inbox and a non-mastodon
software string. Provide `_site_instance` for that, distinct from `_follower`.

- [ ] **Step 1: Write the module docstring**

```python
"""`ban_person` and its four wrappers -- the AP Block and Undo senders.

`app/shared/tasks/blocks.py`, 190 lines. Four `@celery.task` wrappers
(`ban_from_site:41`, `unban_from_site:55`, `ban_from_community:69`,
`unban_from_community:83`) delegating to `ban_person:96`.

THE HANDLER FORKS STRUCTURALLY ON `community_id` AT `:101`, producing two
different envelopes from one function: a community ban addresses the community
and may Announce to its followers, while a site ban addresses the server root
and fans out to every non-mastodon instance. `:130-133` forks again on the same
condition to set `audience`. Combined with `is_undo` that is four shapes before
any delivery path is chosen.

THREE DELIVERY PATHS, AND ONLY THE THIRD USES `following_instances()`:
  `:158-163` site ban -- a RAW `Instance` query filtered only on
      `software != 'mastodon'`, guarded inline by
      `instance.inbox and instance.online() and instance.id != 1`, then returns.
      A recipient here needs NO community membership.
  `:166-168` remote community -- one direct send to `ap_inbox_url`, then returns.
  `:172-190` local communities -- an Announce per community, delivered to each
      `following_instances()` row passing `instance.inbox and
      instance.online()`, plus a FALLBACK send at `:190` to the banned user's
      own instance when it was not already covered.

THE FALLBACK EXISTS FOR A REASON THE COMMENT AT `:189` STATES:
`following_instances()` excludes instances whose only follower was the person
just banned, so their home instance would otherwise never learn of the ban.
Any change to that line has to preserve that case.
"""
```

- [ ] **Step 2: Write imports, constants and helpers**

Transfer the harness from `tests/test_shared_tasks_groups.py` (Phase B's, the
most recent), adapting `_seed` and adding `_site_instance`:

```python
def _seed(local_community=True, with_keys=False):
    """instance, user, mod, community -- committed.

    `user` is the person being banned; `mod` is the person doing it.
    `ban_person` loads both (`:99-100`) and signs with the MOD's key on the
    site-ban and remote-community paths, and with the COMMUNITY's key on the
    local-Announce path -- so `with_keys=True` supplies all three.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    mod = make_user(instance, 'themod', local=True, with_keys=with_keys)
    user = make_user(instance, 'banned', local=True)
    community = make_community('c1')
    if with_keys:
        community.private_key = mod.private_key
        community.public_key = mod.public_key
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, mod=mod,
                           community=community)


def _site_instance(http_mock, domain='peer.example', software='lemmy',
                   inbox=PEER_INBOX):
    """An Instance the SITE-BAN path will deliver to.

    Distinct from `_follower` and deliberately so: `:159` queries Instance
    directly, filtered only on `software != 'mastodon'`, so a site-ban
    recipient needs NO community membership -- only a row with an inbox, a
    non-mastodon software string, and an id that is not 1.

    Returns `(route, instance)`.
    """
    inst = make_instance(domain, software=software)
    inst.inbox = inbox
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst
```

- [ ] **Step 3: Write the two smoke tests**

```python
def test_ban_from_site_fans_out_to_every_non_mastodon_instance(
        db_session, http_mock):
    """`ban_from_site:41` end to end: the instance-ban fork at `:108-112`, the
    Block envelope at `:116-129`, `:133`'s audience, and the site fan-out at
    `:158-163`.

    The Block goes out UNWRAPPED on this path -- there is no Announce -- so no
    `@context` assertion belongs on it: `signature.py:100-101` reinjects at the
    top level, which is exactly where this object sits.
    """
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', False)

    block = _sent_activity(route)
    assert block['type'] == 'Block'
    assert block['object'] == s.user.public_url()
    assert block['actor'] == s.mod.public_url()
    assert block['target'].endswith('/')


def test_ban_from_community_announces_to_the_communitys_followers(
        db_session, http_mock):
    """`ban_from_community:69` on a LOCAL community: the community fork at
    `:102-107`, `:131`'s audience, and the Announce loop at `:172-188`.

    The nested `@context` absence discriminates -- `:154` deletes it from the
    Block before `:179` nests it, and the reinjection never reaches inside.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Block'
    assert '@context' not in announce['object']
```

- [ ] **Step 4: Run** — expect 2 tests.

- [ ] **Step 5: Commit** — subject: `test: open tests/test_shared_tasks_blocks.py with the Block harness`

---

## Task 7: The `local_only` guard, and the production change at `blocks.py:104`

**Files:**
- Modify: `app/shared/tasks/blocks.py:104`
- Modify: `tests/test_shared_tasks_blocks.py`

**Context:** `:104` is `if community.local_only: return` — reached only on the
community-ban fork, and omitting **both** `private` and `online()`.

- [ ] **Step 1: Write the local_only test, then the two failing tests**

Follow Task 3's shape exactly: build the follower inline without a route,
assert `db.session.query(ActivityPubLog).count() == 0`, and separate the
`private` and `online()` cases into two tests because they fail independently.

```python
def test_a_local_only_community_ban_sends_nothing(db_session, http_mock):
    """`:104`'s first disjunct, reached only via the community fork at `:101`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.local_only = True
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_ban_sends_nothing(db_session, http_mock):
    """D309's site in this module, FIRST of two conjuncts this fix adds.

    Before this commit `:104` read `if community.local_only:` alone, omitting
    `private` AND the `instance.online()` check every other site in this family
    already carried -- a WIDER gap than the eight closed before it.

    `private` sits before the `online()` call: `Community.instance_id` is a
    nullable FK and `or` short-circuits left to right, so a private community
    with no instance row returns at the guard rather than raising. This test
    does not assert that; reordering would reopen it without failing here.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    s.community.private = True
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_ban_sends_nothing(db_session, http_mock):
    """The SECOND conjunct. Separated because the two fail independently."""
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    inst = make_instance('follower.example', software='lemmy')
    inst.inbox = PEER_INBOX
    member = make_user(inst, 'member_follower')
    make_community_member(member, s.community)
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run and READ BOTH FAILURES.** Expect `assert 1 == 0` on the two
new tests. That row is a failed-delivery row, not a completed send.

- [ ] **Step 3: Make the production change**

`app/shared/tasks/blocks.py:104`, from `if community.local_only:` to:

```python
        if community.local_only or community.private or not community.instance.online():
```

- [ ] **Step 4: Run** — expect 5 tests.

- [ ] **Step 5: Verify** — numstat `1 1` for `blocks.py`; `wc -l` 190.

- [ ] **Step 6: Commit** — subject: `fix: stop ban_person federating a private or offline community`

---

## Task 8: The four shapes and the three delivery paths

**Files:**
- Modify: `tests/test_shared_tasks_blocks.py`

**Context:** `community_id` × `is_undo` gives four envelope shapes, and there
are three delivery paths. Enumerate which combinations are reachable before
writing — §8's risk 3 in the spec says not all twelve are.

- [ ] **Step 1: Write the undo tests for both forks**

```python
def test_unban_from_site_wraps_the_block_in_an_undo(db_session, http_mock):
    """`unban_from_site:55` -> `is_undo=True` on the instance fork.

    `:136` strips the Block's `@context` before `:142` nests it; `:151` strips
    the Undo's own. On this path the Undo is the top-level object, so the
    reinjection at `signature.py:100-101` supplies one anyway -- which is why
    the NESTED absence is the assertion that discriminates and the top-level
    presence is not asserted at all.
    """
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    unban_from_site(None, s.user.id, s.mod.id, None, 'appealed')

    undo = _sent_activity(route)
    assert undo['type'] == 'Undo'
    assert undo['object']['type'] == 'Block'
    assert '@context' not in undo['object']


def test_unban_from_community_announces_a_nested_undo(db_session, http_mock):
    """`unban_from_community:83` on a local community: three levels, with
    `@context` at the top only.

    `:136` strips the Block, `:151` strips the Undo, and `:180` gives the
    Announce its own. Both inner absences fail independently.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    unban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'ok')

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'Undo'
    assert '@context' not in announce['object']
    assert announce['object']['object']['type'] == 'Block'
    assert '@context' not in announce['object']['object']
```

- [ ] **Step 2: Write the remote-community path test**

```python
def test_a_remote_community_ban_is_sent_direct(db_session, http_mock):
    """`:166-168`. `community.is_local()` is False, so no Announce is built and
    the Block goes straight to the community's own inbox signed with the MOD's
    key rather than the community's.

    `:103` sets `communities = []` for a non-local community, which is why the
    loop at `:172` would have nothing to iterate even if `:168` did not return
    first.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    block = _sent_activity(route)
    assert block['type'] == 'Block'
    assert block['audience'] == s.community.public_url()
```

- [ ] **Step 3: Write the site-ban filter tests**

```python
def test_a_mastodon_instance_receives_no_site_ban(db_session, http_mock):
    """`:159`'s `Instance.software != 'mastodon'` filter.

    NO ROUTE IS REGISTERED for the mastodon instance -- under
    `assert_all_called=True` a registered route that never fires would fail
    this test for the wrong reason, while an unexpected send would surface as
    an unmatched request.
    """
    s = _seed(with_keys=True)
    masto = make_instance('masto.example', software='mastodon')
    masto.inbox = OTHER_INBOX
    db.session.commit()
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', False)

    assert _delivered_inboxes(route) == {PEER_INBOX}


def test_the_local_instance_receives_no_site_ban(db_session, http_mock):
    """`:161`'s `instance.id != 1` conjunct. `_seed` creates the local instance
    first, so it holds id 1 -- the same ordering `following_instances()`
    excludes explicitly and this path excludes by hand."""
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', False)

    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert len(route.calls) == 1
```

- [ ] **Step 4: Run** — expect 10 tests.

- [ ] **Step 5: Commit** — subject: `test: cover ban_person's four shapes and three delivery paths`

---

## Task 9: The fallback send, and the production change at `blocks.py:189`

**Files:**
- Modify: `app/shared/tasks/blocks.py:189`
- Modify: `tests/test_shared_tasks_blocks.py`

**Context:** `:189-190` is the fallback that tells the banned user's own
instance about the ban even when `following_instances()` did not include it:

```python
        if user.instance_id not in sent_to:     # <comment explaining why>
            send_post_request(user.instance.inbox, announce, ...)
```

`:187` guards every other recipient with `instance.inbox and
instance.online()`. This guards nothing.

**TWO FAILURE MODES, AND THE FIX MUST HANDLE BOTH:**
1. `User.instance_id` is a nullable FK and `User.instance` is a `lazy='joined'`
   relationship. With a null `instance_id`, `None not in sent_to` is True and
   `user.instance.inbox` raises `AttributeError`.
2. With an instance row whose `inbox` is null, the send goes to `None`, which
   `post_request` records as an `empty uri` failure without making a request.

**THE COMMENT AT `:189` IS THE SPECIFICATION.** It says
`following_instances()` excludes instances whose only follower was the person
just banned. The fix must keep delivering in that case — a guard that also
dropped the banned user's instance would silently undo the behaviour the
fallback exists to provide.

- [ ] **Step 1: Write the fallback's happy-path test FIRST**

This is the case the comment describes, and it must keep passing.

```python
def test_the_banned_users_own_instance_is_told_even_when_not_a_follower(
        db_session, http_mock):
    """`:189-190`'s fallback, and the behaviour the comment there exists to
    protect.

    The banned user is on an instance that is NOT among the community's
    followers, so `following_instances()` never returns it and `sent_to` never
    gains its id -- yet it must still learn of the ban. This test is written
    BEFORE the guard is added and must keep passing after it.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    follower_route, _f = _follower(s, http_mock)
    home = make_instance('home.example', software='lemmy')
    home.inbox = OTHER_INBOX
    db.session.commit()
    s.user.instance_id = home.id
    db.session.commit()
    home_route = http_mock.post(OTHER_INBOX).respond(200, json={})

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert _delivered_inboxes(follower_route, home_route) == {PEER_INBOX,
                                                              OTHER_INBOX}
```

- [ ] **Step 2: Write the two failing tests**

```python
def test_a_banned_user_with_no_instance_row_does_not_crash(
        db_session, http_mock):
    """`:190`'s first unguarded dereference. `User.instance_id` is a nullable
    FK, so `None not in sent_to` is True and `user.instance.inbox` raises
    AttributeError on None.

    This test FAILS before the guard with that AttributeError propagating out
    of the wrapper, and passes after.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _f = _follower(s, http_mock)
    s.user.instance_id = None
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert _delivered_inboxes(route) == {PEER_INBOX}


def test_a_banned_users_instance_without_an_inbox_is_not_posted_to(
        db_session, http_mock):
    """`:190`'s second unguarded dereference. With an instance row whose inbox
    is null, the send goes to None, which `post_request` records at
    `signature.py:109-111` as an `empty uri` failure WITHOUT making an httpx
    request.

    THE ROW COUNT IS THE ASSERTION, not the delivered inboxes. A send to None
    leaves the inbox set unchanged and the route call count unchanged, so only
    the ActivityPubLog count can see it: one row for the real follower, and a
    second for the None-inbox attempt if the guard is missing.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _f = _follower(s, http_mock)
    home = make_instance('home.example', software='lemmy')
    home.inbox = None
    db.session.commit()
    s.user.instance_id = home.id
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert db.session.query(ActivityPubLog).count() == 1
```

- [ ] **Step 3: Run and READ BOTH FAILURES.** The first should fail with an
`AttributeError`, the second with `assert 2 == 1`. If either differs, stop.

- [ ] **Step 4: Make the production change**

`app/shared/tasks/blocks.py:189`, from:

```python
        if user.instance_id not in sent_to:     # <existing comment>
```

to:

```python
        if user.instance_id not in sent_to and user.instance and user.instance.inbox and user.instance.online():     # <existing comment>
```

**KEEP IT ON ONE LINE AND PRESERVE THE TRAILING COMMENT.** `:189` is already
225 characters because of that comment, and `:188`/`:190` are 120 and 121, so
the file's own convention accommodates it. Wrapping would turn a one-line
modification into an insertion and change the file's length, which every
mutation restore has to assert against.

Re-derive the line and its indentation.

- [ ] **Step 5: Run** — expect 13 tests, including Step 1's, which must still pass.

- [ ] **Step 6: Verify** — `git diff --numstat -- app/shared/tasks/blocks.py`
must be `2 2` for the sub-project so far (this change plus Task 7's);
`wc -l app/shared/tasks/blocks.py` must still be 190.

- [ ] **Step 7: Commit** — subject: `fix: guard the ban fallback on the banned user's instance`

---

## Task 10: Close `blocks.py`, mutate, set its floor

**Files:**
- Modify: `tests/test_shared_tasks_blocks.py`
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Cover the remaining arms**

```python
def test_a_null_expiry_becomes_the_year_2100(db_session, http_mock):
    """`:98`, guarded by `:97`. A ban with no expiry is federated as one
    expiring in 2100 rather than as one with no `endTime` -- the wire format
    has no way to say "never", so the code picks a date far enough out to mean
    it."""
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', False)

    block = _sent_activity(route)
    assert block['endTime'].startswith('2100-01-01')
    assert block['expires'] == block['endTime']


def test_an_explicit_expiry_is_used_unchanged(db_session, http_mock):
    """`:97`'s false arm. The control for the test above: without it, the 2100
    default is never distinguished from an unconditional one."""
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)
    expiry = datetime.datetime(year=2030, month=6, day=1)

    ban_from_site(None, s.user.id, s.mod.id, expiry, 'spam', False)

    block = _sent_activity(route)
    assert block['endTime'].startswith('2030-06-01')


def test_remove_data_is_carried_on_the_wire(db_session, http_mock):
    """`:127`'s `removeData` with a True value. `ban_from_site` is the only
    wrapper that passes it through -- the other three hardcode False at `:60`,
    `:74` and `:88` -- so this is the one path where a caller's choice
    reaches the activity."""
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', True)

    assert _sent_activity(route)['removeData'] is True


def test_remove_data_false_is_carried_too(db_session, http_mock):
    """The other arm of the same field. Asserting `is False` rather than
    falsiness, because a missing key would also read as falsy."""
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    ban_from_site(None, s.user.id, s.mod.id, None, 'spam', False)

    assert _sent_activity(route)['removeData'] is False


def test_a_community_undo_carries_the_communitys_audience(
        db_session, http_mock):
    """`:148`, guarded by `:147`. The Undo gets an `audience` only on the
    community fork; the site fork leaves it off entirely."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    unban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'ok')

    undo = _sent_activity(route)['object']
    assert undo['audience'] == s.community.public_url()


def test_a_site_undo_carries_no_audience(db_session, http_mock):
    """`:147`'s false arm. The control: without it, `audience` being present
    on a community undo is never distinguished from it being unconditional."""
    s = _seed(with_keys=True)
    route, _inst = _site_instance(http_mock)

    unban_from_site(None, s.user.id, s.mod.id, None, 'appealed')

    assert 'audience' not in _sent_activity(route)


def test_a_community_follower_without_an_inbox_is_skipped(
        db_session, http_mock):
    """`:187`'s `instance.inbox` conjunct, with a second follower proving the
    loop CONTINUES.

    THE ActivityPubLog COUNT IS THE DISCRIMINATING ASSERTION, for the reason
    Task 9's inbox test gives: a send to a None inbox produces a row at
    `app/activitypub/signature.py:105` and takes the `if uri is None` arm at
    `:109` WITHOUT making an httpx request, so respx sees nothing and a
    delivered-inboxes assertion alone cannot see the difference.

    The inboxless follower is created FIRST so it takes the lower id. If that
    query-plan assumption breaks, this test degrades to LAX -- passing under an
    aborting loop -- never to FLAKY.

    The banned user is put on the good follower's instance so the fallback at
    `:189` does not fire and add a second row this assertion would have to
    account for.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    dud = make_instance('inboxless.example', software='lemmy')
    dud.inbox = None
    dud_member = make_user(dud, 'member_inboxless')
    make_community_member(dud_member, s.community)
    db.session.commit()
    route, good = _follower(s, http_mock, inbox=OTHER_INBOX,
                            domain='good.example')
    s.user.instance_id = good.id
    db.session.commit()

    ban_from_community(None, s.user.id, s.mod.id, s.community.id, None, 'spam')

    assert _delivered_inboxes(route) == {OTHER_INBOX}
    assert db.session.query(ActivityPubLog).count() == 1
```

Add `import datetime` to the top of the file, and re-derive the import block
after your edit is final.

- [ ] **Step 2: Measure** with `--cov=app.shared.tasks.blocks`, retrieving the
JSON with `podman cp`.

- [ ] **Step 3: Close any residual**, or document a proof of unreachability.

- [ ] **Step 4: Mutate**

| # | Target | Mutation | Expected |
|---|---|---|---|
| M9 | `:104` | drop `community.private or` | killed by `test_a_private_community_ban_sends_nothing` |
| M10 | `:104` | drop `not community.instance.online()` | killed by `test_an_offline_community_instance_ban_sends_nothing` |
| M11 | `:159` | drop the `software != 'mastodon'` filter | killed by `test_a_mastodon_instance_receives_no_site_ban` |
| M12 | `:161` | drop `instance.id != 1` | killed by `test_the_local_instance_receives_no_site_ban` |
| M13 | `:187` | change `instance.inbox and` to `True and` | killed by the `:187` test from Step 1 |
| M14 | `:189` | drop `and user.instance.inbox` from the new guard | killed by `test_a_banned_users_instance_without_an_inbox_is_not_posted_to` |
| M15 | `:189` | drop `and user.instance` from the new guard | killed by `test_a_banned_user_with_no_instance_row_does_not_crash` |
| M16 | `:189` | replace the whole condition with `if False:` | killed by `test_the_banned_users_own_instance_is_told_even_when_not_a_follower` |

M16 is the one that proves the fix did not break what the fallback exists for.
If it survives, the guard has silently disabled the fallback and Step 1's test
is not doing its job.

- [ ] **Step 5: Add the floor** — `app/shared/tasks/blocks.py = 100`

- [ ] **Step 6: Prove it bites** by inversion with an isolation control.

- [ ] **Step 7: Commit** — subject: `test: close app/shared/tasks/blocks.py and set its floor`

---

## Task 11: Register the findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

**Context:** Next free number is **D328** — confirm against the register. Facts
end at **145**; append from **146**.

- [ ] **Step 1: Add the new entries**

- **The fallback send's missing guard** (`blocks.py:189-190`) — fixed by
  production change 3. Record BOTH failure modes: a null `instance_id` raising
  `AttributeError`, and a null `inbox` producing an `empty uri` row with no
  request. Record that `following_instances()`'s exclusion of the just-banned
  user's instance is WHY the fallback exists, so the fix preserves it.
- **`blocks.py:143`'s dead `@context` assignment** — `:151` deletes it eight
  lines later. Harmless: on the site-ban and remote paths the Undo is top-level
  and `signature.py:100-101` reinjects an identical value; on the local path it
  is nested, where absence is correct. **Say why its removal would be invisible
  to tests**, so a reader does not mistake that for a coverage gap.
- **`blocks.py:159`'s raw `Instance` query is NOT another D321.** It bypasses
  `following_instances()` but applies `online()` and `id != 1` inline, so the
  dormant/gone-forever filtering happens anyway. Record the distinction
  explicitly — the surface shape is identical to D321's and a reader
  pattern-matching on "raw Instance query" would file a duplicate.

- [ ] **Step 2: Make the in-place edits**

- **D309** drops from three files to **one** (`deletes.py`). State the counting
  unit. **AND RECORD THAT `blocks.py:104` AND `groups.py:59` WERE WIDER GAPS
  THAN THE REST OF THE FAMILY** — every other site omitted only `private`;
  these two omitted `private` AND `online()`. **Check `deletes.py:127` and
  `:130` and record which shape they are**; that costs one grep and tells the
  next round what it is walking into.
- **The unused-import finding** that already covers `adds.py`/`removes.py`'s
  `post_request` gains `groups.py`, which imports `post_request` at `:2` and
  `get_comm_flair_list`/`comm_flair_ap_format` at `:5`, using none of the
  three. **Read the register and find which entry carries that shape**; extend
  it rather than allocating.
- **D324** — recount `_recording_task_session` copies with
  `grep -rn "def _recording_task_session" tests/` and update if this
  sub-project added any. It may not have; the three new files use
  `get_task_session` differently.

- [ ] **Step 3: Append the harness facts from 146**

- **146** — `task_selector`'s two dispatch arms are indistinguishable under
  `task_always_eager` except by RETURN VALUE. `:66` calls `.delay()` and falls
  off the end returning `None`; `:68` calls the task directly and returns its
  value. A test asserting only that the task executed passes under either arm.
  The technique that works: patch the task's own module attribute with a stub
  returning a sentinel — `task_selector` imports inside its body, so the names
  resolve at call time and the stub reaches the dict. The stub cannot exercise
  `:66`, which needs a real Celery task for `.delay()`.
- **147** — a site-wide fan-out and a community fan-out need different
  fixtures. `blocks.py:159` queries `Instance` directly with only a software
  filter, so a site-ban recipient needs no `CommunityMember` row — while every
  `following_instances()` path does (fact 141). A helper built for one will
  silently produce zero recipients for the other.

- [ ] **Step 4: Verify every citation you wrote** — open the line, confirm it
carries the claimed statement. Re-derive citations into files your own diff
touches after the diff is final.

- [ ] **Step 5: Commit** — subject: `docs: register sub-project 26's findings and harness facts`

---

## Final verification (controller only)

- [ ] Full suite, **foreground and unpiped**, with coverage.
- [ ] Collected counts from collection for all three new files.
- [ ] `git diff --numstat <base>..HEAD -- app/` — expect `1 1` for `groups.py`
  and `2 2` for `blocks.py`, with `__init__.py` untouched.
- [ ] `wc -l` — `groups.py` 149, `blocks.py` 190.
- [ ] Floors: 17 entries, all met, against a report whose mtime postdates the run.
- [ ] `git status --short` — clean.

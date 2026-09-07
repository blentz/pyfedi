# Sub-project 24: `flags.py` and `users.py` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close `app/shared/tasks/flags.py` (42 stmts, 22.9167%) and
`app/shared/tasks/users.py` (58 stmts, 12.8205%) to zero missing statements and
zero partial branches, landing three one-line production changes on the way.

**Architecture:** Two test files, built in that order. `tests/test_shared_tasks_flags.py`
transfers the harness the campaign built across `notes.py`, `pages.py`, `adds.py`
and `removes.py` — `_seed`, the autouse resolver stub, `_remote_inbox`,
`_sent_activity`, `_recording_task_session`. `tests/test_shared_tasks_users.py`
builds a new one, because `users.py` is not federation-shaped: it needs a
recording HTTP double, a suppressed `sleep`, and a controlled `random`.

**Tech Stack:** pytest, respx, SQLAlchemy 2.0.52, Flask, Celery.

**Spec:** `docs/superpowers/specs/2026-09-07-coverage-flags-users-24-design.md`

## Global Constraints

- **Delete nothing the task did not create.** `claude_test` in the repository root is not this campaign's.
- Only the controller runs the full suite, one pytest session at a time, **in the foreground**.
- `pytest` exits 1 on a session timeout and `run_tests.sh` propagates it. A shell **pipeline** eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- Coverage takes the **dotted module form**: `--cov=app.shared.tasks.flags`. A path form collects nothing, writes no JSON, and exits 0 — it fails silently and green.
- **Test counts come from collection**, never from `grep -c '^def test_'`.
- Before believing any failure, run `./run_tests.sh --down` — a wedged podman stack reports failures that are not regressions.
- **Mutations:** one at a time, targeted single-line `sed`, **dry-run without `-i` and read the produced line first**, restore with `git checkout -- app/`, then assert both an empty `git diff -- app/` and the expected `wc -l`.
- Commit with `git commit -F <file>`, never `-m`.
- Every line number re-derived against the current tree. Anchor every HEAD-relative number to the commit it was true at.
- **No ordered assertions over delivered instances.** Set-based or sorted, always. Sub-project 23 spent a ruling removing the last two from the suite.
- Per fact 132, decide class-level vs instance-level monkeypatching **per call site** by reading whether the production path re-loads the object. Confirm with a raising probe. Never copy the form from a neighbouring file.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_shared_tasks_flags.py` | **Create.** All `flags.py` coverage. Transfers the federation harness. |
| `tests/test_shared_tasks_users.py` | **Create.** All `users.py` coverage. New non-federation harness. |
| `tests/factories.py` | **Modify.** Add `make_user_registration`. |
| `app/shared/tasks/flags.py:57` | **Modify.** Production change 1 (D309). |
| `app/shared/tasks/users.py:80-81` | **Modify.** Production change 2 (D319). |
| `app/shared/tasks/users.py` email leg | **Modify.** Production change 3 (D320). |
| `coverage_floors.ini` | **Modify.** Two new entries. |
| `tests/README.md` | **Modify.** Facts 133-135. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify.** D319-D321, in-place edits. |

---

## Task 1: Open `tests/test_shared_tasks_flags.py` with the transferred harness

**Files:**
- Create: `tests/test_shared_tasks_flags.py`

**Interfaces:**
- Produces: `_seed(local_community=True, with_keys=False)` returning
  `SimpleNamespace(instance, user, community, post, reply, reporter)`;
  `_peer(domain='peer.example', software='lemmy')`; `_make_deliverable(s, inbox)`;
  `_reporting_instance(s, http_mock, inbox)` returning a respx route;
  `_sent_activity(route, index=-1)`. Tasks 2-6 consume all of these.

**Context:** `flags.py` has two `@celery.task` wrappers delegating to one shared
handler — the sixth instance of a shape closed five times. The harness below is
transferred from `tests/test_shared_tasks_add_remove.py:1-205`, adapted.

**THE ONE CITATION THAT MUST CHANGE ON THE WAY ACROSS.** The source docstring
says `post_request` "builds and `session.add`s its `ActivityPubLog` row
UNCONDITIONALLY at app/activitypub/signature.py:102". **The add is at `:105`;
`:102` is an unrelated `type = ...` statement.** This is sub-project 23's parked
Ruling 3. Write `:105` in the new file. Do not propagate the error, and do not
edit the old file — that is a separate concern.

- [ ] **Step 1: Write the module docstring and imports**

```python
"""`report_post` and `report_reply` -- the AP Flag senders.

`app/shared/tasks/flags.py`, 78 lines, three functions: two `@celery.task`
wrappers (`report_reply:25`, `report_post:40`) delegating to `report_object:54`.
This is the SIXTH module in which the campaign has met this shape, after
`notes.py`, `pages.py`, `adds.py` and `removes.py`, and the harness below is
transferred from `tests/test_shared_tasks_add_remove.py` rather than rebuilt.

THE TWO WRAPPER LOOKUPS DIFFER, FIFTEEN LINES APART, IN ONE FILE:
`:30` is `session.query(PostReply).filter_by(id=reply_id).one()` and raises
`NoResultFound` for a missing id; `:45` is `session.query(Post).get(post_id)`
and returns None, so the raise arrives later as `AttributeError` when `:56`
reads `object.community`. Establish each by READING, never by inheriting from
its neighbour -- this is the fourth module in which the campaign has met this
trap, and sub-project 21 lost a fix round to it.

WHAT DOES NOT APPLY HERE. `@context` is set at `:67`, inside the `flag` dict,
and `flag` IS the top-level posted object -- there is no Announce wrapper in
this module. The nested-`@context`-absence assertions that carried sub-projects
20-23 have no subject here, and asserting the absence would be asserting a
property of a structure that does not exist.

ORDER IS NOT GUARANTEED AND IS NEVER ASSERTED. `:73` runs
`session.query(Instance).filter(Instance.id.in_(instance_ids))` through the
TASK session, so the rows are neither the test's objects nor in any promised
order. Every assertion over delivered inboxes in this file is set-based.
Sub-project 23 spent a ruling removing the last two ordered assertions from
this suite; this file does not add a third.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.flags import report_post, report_reply
from tests.factories import (
    make_community, make_instance, make_post, make_post_reply, make_user,
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
    """instance, reporter, community, post, reply -- committed.

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
    reporter = make_user(instance, 'reporter', local=True, with_keys=with_keys)
    author = make_user(instance, 'author', local=True)
    community = make_community('c1')
    post = make_post(community, author, ap_id='https://test.piefed.local/post/1')
    reply = make_post_reply(post, author, body='a reply')
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=reporter, author=author,
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

- [ ] **Step 3: Write the capture mechanism**

```python
def _make_deliverable(s, online=True):
    """Put the community on a real peer Instance so `:57`'s gate passes.

    The database half of `_reporting_instance`. The early-return tests call
    this ALONE, because they assert the delivery never happens and `http_mock`
    is built with `assert_all_called=True` -- a registered route that never
    fires would fail them for the wrong reason.

    `Instance.online()` (app/models.py:118-119) is exactly
    `not (self.dormant or self.gone_forever)`, so `online=False` sets both.
    """
    peer = _peer()
    if not online:
        peer.dormant = True
        peer.gone_forever = True
    s.community.instance_id = peer.id
    db.session.commit()
    return peer


def _reporting_instance(s, http_mock, inbox=PEER_INBOX, domain='recipient.example'):
    """THE CAPTURE MECHANISM. Returns (route, instance_id).

    `report_object` does NOT deliver to the community's instance -- it delivers
    to the instances named in `instance_ids`, queried at `:73`. So the
    community's instance controls only the `:57` gate; the RECIPIENT is a
    separate Instance row created here.

    Three things must be true for `:76`'s call to become an observable request:
    `:57`'s gate must pass (`_make_deliverable`), the recipient Instance must
    have an `inbox`, and the reporter must have a keypair
    (`_seed(with_keys=True)`), because signing calls `.encode()` on
    `user.private_key`.

    WHY THE INBOX MATTERS IS NOT THAT `post_request` "SHORT-CIRCUITS" ON A
    MISSING ONE -- that verb would make this file's early-return assertions
    look vacuous. `post_request` builds and `session.add`s its
    `ActivityPubLog` row UNCONDITIONALLY at app/activitypub/signature.py:105,
    BEFORE the transport and before the uri check at :109-111, which does not
    return early either: it marks the already-written row `failure` /
    `empty uri`. So a row is written for ANY attempted delivery, including one
    to a None inbox. That is exactly what makes
    `assert db.session.query(ActivityPubLog).count() == 0` a real observation
    -- it distinguishes "the guard returned before `:73`" from "the loop ran
    and delivered nowhere", which a short-circuit reading would say it cannot.

    NOTE THE LINE NUMBER. Sub-project 23's copy of this docstring cited `:102`
    for the `session.add`; `:102` is an unrelated `type = ...` statement and
    the add is at `:105`. Corrected here.

    WHY THE HTTP BODY AND NOT A RECORDER: respx captures the bytes that
    actually left, at app/activitypub/signature.py:494. A recorder holding the
    dict would be read back after any in-place mutation.
    """
    recipient = make_instance(domain, software='lemmy')
    recipient.inbox = inbox
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), recipient.id


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call."""
    return json.loads(route.calls[index].request.content)


def _delivered_inboxes(*routes):
    """The SET of inboxes that received a request. Never a list, never ordered
    -- see the module docstring."""
    return {str(r.calls[i].request.url) for r in routes for i in range(len(r.calls))}
```

- [ ] **Step 4: Write the two smoke tests**

```python
def test_report_post_delivers_a_flag_to_the_named_instance(
        db_session, http_mock):
    """`report_post:40` end to end: `.get()` at `:45`, the gate at `:57`
    passing, the envelope at `:60-71`, and the loop at `:73-76` delivering.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)

    report_post(None, s.user.id, s.post.id, 'spam', [recipient_id])

    flag = _sent_activity(route)
    assert flag['type'] == 'Flag'
    assert flag['summary'] == 'spam'
    assert flag['object'] == s.post.public_url()
    assert flag['actor'] == s.user.public_url()


def test_report_reply_delivers_a_flag_naming_the_reply(db_session, http_mock):
    """`report_reply:25` end to end. Written out separately from
    `report_post`'s test rather than parametrised: a parametrised failure
    names the parameter rather than the function, and an arm covered under
    only one parameter is invisible in the failure output.

    The assertion that earns this test its place is `flag['object']` -- it is
    the REPLY's url, not the post's, which is what distinguishes `:30`'s
    lookup from `:45`'s reaching the same handler.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)

    report_reply(None, s.user.id, s.reply.id, 'abuse', [recipient_id])

    flag = _sent_activity(route)
    assert flag['type'] == 'Flag'
    assert flag['object'] == s.reply.public_url()
    assert flag['object'] != s.post.public_url()
```

- [ ] **Step 5: Run the two tests**

Run: `./run_tests.sh tests/test_shared_tasks_flags.py -v`
Expected: PASS, 2 tests. If `make_post_reply`'s signature differs from
`make_post_reply(post, user, body='a reply')` (`tests/factories.py:453`),
re-derive it and adjust — do not guess.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_tasks_flags.py
git commit -F <message-file>
```
Message subject: `test: open tests/test_shared_tasks_flags.py with the Flag harness`

---

## Task 2: The gate at `:57` — the two arms that exist today

**Files:**
- Modify: `tests/test_shared_tasks_flags.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable`, `_peer` from Task 1.

**Context:** `:57` reads
`if community.local_only or not community.instance.online(): return`. Two
conditions today; Task 3 adds the third. Each early return is proved by **two**
observations, not one: zero outbound requests **and** zero `ActivityPubLog`
rows. The second is what distinguishes "returned before `:73`" from "the loop
ran and delivered nowhere" — see `_reporting_instance`'s docstring.

- [ ] **Step 1: Write the two failing-if-broken tests**

```python
def test_a_local_only_community_sends_no_flag(db_session, http_mock):
    """`:57`'s first disjunct. `local_only` returns before the envelope is
    built, so nothing is sent AND no ActivityPubLog row is written.

    NO ROUTE IS REGISTERED. `http_mock` is built with
    `assert_all_called=True`, so a route registered here and never called
    would fail this test for the wrong reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    recipient = make_instance('recipient.example', software='lemmy')
    recipient.inbox = PEER_INBOX
    s.community.local_only = True
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [recipient.id])

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_no_flag(db_session, http_mock):
    """`:57`'s second disjunct. `Instance.online()` (app/models.py:118-119) is
    `not (self.dormant or self.gone_forever)`, so a dormant-and-gone instance
    returns.

    THE CONTROL FOR THIS TEST IS Task 1's SMOKE TEST, which runs the same path
    with `online=True` and DOES deliver. Without that pairing, an assertion of
    zero rows passes against any breakage that stops delivery for any reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    recipient = make_instance('recipient.example', software='lemmy')
    recipient.inbox = PEER_INBOX
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [recipient.id])

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run them**

Run: `./run_tests.sh tests/test_shared_tasks_flags.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 3: Prove both assertions can fail**

Mutate `:57` to remove the gate entirely, dry-run first:

```bash
sed -n '57p' app/shared/tasks/flags.py
sed '57s/.*/    if False:/' app/shared/tasks/flags.py | sed -n '57p'
```
Read the produced line. Then apply with `-i`, run the two tests, and confirm
BOTH now fail. Restore:

```bash
git checkout -- app/
git diff -- app/          # must be empty
wc -l app/shared/tasks/flags.py   # must be 78
```

- [ ] **Step 4: Commit**

Message subject: `test: cover the Flag gate's local_only and offline arms`

---

## Task 3: Production change 1 — the `private` conjunct at `:57` (D309)

**Files:**
- Modify: `app/shared/tasks/flags.py:57`
- Modify: `tests/test_shared_tasks_flags.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable` from Task 1.

**Context:** D309 is the campaign's federation-gate family: a guard that tests
`local_only` and `online()` but not `Community.private`, so a private
community's activity federates out. `flags.py:57` is one of its remaining
sites. Closing it drops the count from six files to five.

- [ ] **Step 1: Write the failing test**

```python
def test_a_private_community_sends_no_flag(db_session, http_mock):
    """D309's site in this module. `:57` gates on `local_only` and
    `instance.online()` but not on `Community.private`, so a report about
    content in a private community federates out.

    This test FAILS before the conjunct is added -- a real ActivityPubLog row
    is written and a real request is attempted -- and passes after.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    recipient = make_instance('recipient.example', software='lemmy')
    recipient.inbox = PEER_INBOX
    s.community.private = True
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [recipient.id])

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run it and read the failure**

Run: `./run_tests.sh tests/test_shared_tasks_flags.py::test_a_private_community_sends_no_flag -v`
Expected: FAIL on `assert 1 == 0`. **Read the failure and confirm it is the
count assertion**, not a fixture error, an `AttributeError` on
`Community.private`, or an unmatched-route error from respx. A test that fails
for the wrong reason proves nothing when it later passes.

- [ ] **Step 3: Make the production change**

`app/shared/tasks/flags.py:57`, from:

```python
    if community.local_only or not community.instance.online():
```

to:

```python
    if community.local_only or community.private or not community.instance.online():
```

- [ ] **Step 4: Run the whole file**

Run: `./run_tests.sh tests/test_shared_tasks_flags.py -v`
Expected: PASS, 5 tests. The new test passes; the other four are unchanged.

- [ ] **Step 5: Verify the diff is exactly one line**

```bash
git diff --numstat -- app/shared/tasks/flags.py   # must be: 1  1
wc -l app/shared/tasks/flags.py                    # must be 78
```

- [ ] **Step 6: Commit**

```bash
git add app/shared/tasks/flags.py tests/test_shared_tasks_flags.py
git commit -F <message-file>
```
Message subject: `fix: stop report_object federating a private community`

---

## Task 4: The two lookup styles, and both wrappers' `except`/`finally`

**Files:**
- Modify: `tests/test_shared_tasks_flags.py`

**Interfaces:**
- Consumes: `_seed` from Task 1.
- Produces: `_recording_task_session(monkeypatch)` returning
  `SimpleNamespace(calls=[])`. Task 6 consumes it.

**Context:** `:30` `.filter_by(id=reply_id).one()` raises `NoResultFound`;
`:45` `.get(post_id)` returns `None`, so `:56` `object.community` raises
`AttributeError`. Both raises must be **natural** — construct the missing-row
condition, never inject an exception. The wrappers' `except` arms
(`:32-34`, `:47-49`) and `finally` (`:35-36`, `:50-51`) are then observed by
recording the real session's `rollback` and `close`.

- [ ] **Step 1: Add `_recording_task_session`**

```python
def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    A third copy of the helper introduced in
    `tests/test_shared_tasks_send_reply.py:1637` and copied into
    `tests/test_shared_tasks_send_answer.py:567`. Duplicated rather than
    imported: this campaign keeps its test modules independent so a helper can
    be edited for one function's needs without silently changing another's
    assertions. THAT THERE ARE NOW THREE COPIES IS A REGISTERED FINDING.

    The session is real and does real work -- only the observation is added, by
    wrapping the two methods rather than replacing the object. A fake session
    would prove the wrapper calls methods on a mock; this proves it calls them
    on the session the function actually used.

    THE PATCH TARGET IS THE FLAGS MODULE, NOT `app.utils`.
    `app/shared/tasks/flags.py:4` imports `get_task_session` into the flags
    namespace, and both wrappers resolve it there (`:27`, `:42`). Patching
    `app.utils.get_task_session` would apply cleanly, observe nothing, and
    leave the assertion trivially true against an empty list -- the silent
    failure mode this docstring exists to prevent.

    Returns a `SimpleNamespace(calls=[])`; the wrapper appends 'rollback' and
    'close' in the order they happened, so `finally` running after `except` is
    observable rather than assumed.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.flags as flags_module

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

    monkeypatch.setattr(flags_module, 'get_task_session', _make)
    return record
```

- [ ] **Step 2: Write the four tests**

```python
def test_a_missing_reply_raises_NoResultFound_and_rolls_back(
        db_session, monkeypatch):
    """`:30`'s `.filter_by(id=reply_id).one()` against an absent id, plus
    `:32-34`'s except arm and `:35-36`'s finally.

    `.one()` raises `NoResultFound` AT THE LOOKUP -- the handler is never
    entered. Contrast the post wrapper's test below, which reaches `:56`.

    Asserting `['rollback', 'close']` rather than merely `raises` is what
    makes this a test of the wrapper: a `raises`-only test cannot tell a
    rollback from its absence. The ORDER also distinguishes `finally` running
    after `except` from a wrapper that closed instead of rolling back.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(NoResultFound):
        report_reply(None, s.user.id, s.reply.id + 1000, 'spam', [])

    assert record.calls == ['rollback', 'close']


def test_a_missing_post_raises_AttributeError_and_rolls_back(
        db_session, monkeypatch):
    """`:45`'s `.get(post_id)` against an absent id, plus `:47-49` and
    `:50-51`.

    THE DIFFERENT EXCEPTION TYPE IS THE POINT. `.get()` returns None rather
    than raising, so the failure arrives fifteen lines later at `:56`
    (`object.community` on None) as `AttributeError`. Two lookup styles, one
    file. Asserting the type is what records the asymmetry.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        report_post(None, s.user.id, s.post.id + 1000, 'spam', [])

    assert record.calls == ['rollback', 'close']


def test_report_reply_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """`:35-36`'s finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the error tests above: without it, `finally` running is
    only ever observed alongside an exception, and a wrapper that closed only
    in the `except` arm would pass every other test in this file.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)
    record = _recording_task_session(monkeypatch)

    report_reply(None, s.user.id, s.reply.id, 'spam', [recipient_id])

    assert record.calls == ['close']


def test_report_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """`:50-51`'s finally on the SUCCESS path. Written out separately from the
    reply wrapper's rather than parametrised -- the two `finally` blocks are
    different lines in different functions, and a parametrised pass would
    cover one of them under a name that does not say which.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)
    record = _recording_task_session(monkeypatch)

    report_post(None, s.user.id, s.post.id, 'spam', [recipient_id])

    assert record.calls == ['close']
```

- [ ] **Step 3: Probe that the patch is load-bearing (fact 132)**

Both wrappers call `get_task_session` in their own namespace, so the patch
should intercept — but **prove it**. Temporarily make `_make` raise
`RuntimeError('probe')` instead of returning a session, run the four tests, and
confirm all four now fail with that error. A test that passes with a raising
stub was never intercepting. Revert the probe and confirm the file is
byte-identical before proceeding.

- [ ] **Step 4: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_flags.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

Message subject: `test: cover both Flag wrappers' lookups and session teardown`

---

## Task 5: The delivery loop at `:73-76`

**Files:**
- Modify: `tests/test_shared_tasks_flags.py`

**Interfaces:**
- Consumes: `_seed`, `_make_deliverable`, `_reporting_instance`,
  `_sent_activity`, `_delivered_inboxes` from Task 1.

**Context:** `:73` queries `Instance` by `instance_ids`; `:75` skips rows whose
`inbox is None`; `:76` delivers. Three behaviours to cover: the filter actually
filters, a None inbox is skipped, and the loop **continues past a skip**.

**Ordering discipline:** `.in_()` promises no order and the rows come from the
task session. Use `_delivered_inboxes()` and compare **sets**. Do not assert a
list.

- [ ] **Step 1: Write the four tests**

```python
def test_only_the_named_instances_receive_the_flag(db_session, http_mock):
    """`:73`'s `Instance.id.in_(instance_ids)` filter. An instance that exists
    and has an inbox but is NOT named receives nothing.

    Its route is deliberately not registered: under
    `http_mock(assert_all_called=True)` an unregistered inbox that IS posted
    to fails the test as an unmatched request, which is the observation.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, named_id = _reporting_instance(s, http_mock)
    unnamed = make_instance('unnamed.example', software='lemmy')
    unnamed.inbox = OTHER_INBOX
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [named_id])

    assert _delivered_inboxes(route) == {PEER_INBOX}


def test_an_instance_without_an_inbox_is_skipped(db_session, http_mock):
    """`:75`'s `instance.inbox is not None` guard, alone.

    NO ROUTE IS REGISTERED, and the ActivityPubLog count is what proves the
    skip. Per `_reporting_instance`'s docstring, `post_request` writes its row
    unconditionally at app/activitypub/signature.py:105 -- so a count of 0
    means `:76` was never reached, not merely that delivery failed.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inboxless = make_instance('inboxless.example', software='lemmy')
    inboxless.inbox = None
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [inboxless.id])

    assert db.session.query(ActivityPubLog).count() == 0


def test_the_loop_continues_past_an_instance_without_an_inbox(
        db_session, http_mock):
    """`:74`'s loop CONTINUES after `:75` skips one row.

    This is the test the `:75` guard's coverage needs and the one above cannot
    give: with a single inboxless instance, a loop that ABORTED on the skip and
    a loop that CONTINUED past it are indistinguishable. Two rows, one skipped
    and one delivered, separate them.

    THE ORDER IS NOT CONTROLLED AND IS NOT ASSERTED. `:73` returns rows through
    the task session in whatever order the planner chooses, so this asserts
    the SET of delivered inboxes. Whichever row comes first, exactly one
    delivery must land.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, good_id = _reporting_instance(s, http_mock)
    inboxless = make_instance('inboxless.example', software='lemmy')
    inboxless.inbox = None
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [inboxless.id, good_id])

    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert len(route.calls) == 1


def test_two_instances_with_inboxes_both_receive_the_flag(
        db_session, http_mock):
    """`:74`'s loop delivering more than once -- the arc back to the top.

    Set-based, for the reason the module docstring gives.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    first, first_id = _reporting_instance(s, http_mock)
    second, second_id = _reporting_instance(
        s, http_mock, inbox=OTHER_INBOX, domain='second.example')

    report_post(None, s.user.id, s.post.id, 'spam', [first_id, second_id])

    assert _delivered_inboxes(first, second) == {PEER_INBOX, OTHER_INBOX}
```

- [ ] **Step 2: Write the envelope test**

```python
def test_the_flag_carries_a_top_level_context_and_the_community_audience(
        db_session, http_mock):
    """`:60-71`'s envelope, asserted on the bytes that actually left.

    `@context` is at `:67`, INSIDE the flag dict -- and here the flag IS the
    top-level posted object, because this module has no Announce wrapper. So
    the assertion is that `@context` is PRESENT at the top level, which is the
    opposite of the nested-absence assertions sub-projects 20-23 made about
    Announce-wrapped activities.

    `app/activitypub/signature.py:100-101` reinjects `@context` top-level only;
    that is consistent with, and invisible against, what `:67` already set.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)

    report_post(None, s.user.id, s.post.id, 'spam', [recipient_id])

    flag = _sent_activity(route)
    assert '@context' in flag
    assert flag['audience'] == s.community.public_url()
    assert flag['to'] == [s.community.public_url()]
    assert flag['id'].startswith('https://test.piefed.local/activities/flag/')
```

- [ ] **Step 3: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_flags.py -v`
Expected: PASS, 14 tests.

- [ ] **Step 4: Commit**

Message subject: `test: cover the Flag delivery loop's filter, skip and repeat`

---

## Task 6: Close `flags.py`, mutate, and set its floor

**Files:**
- Modify: `tests/test_shared_tasks_flags.py` (only if coverage shows gaps)
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: everything from Tasks 1-5.

- [ ] **Step 1: Measure**

```bash
./run_tests.sh tests/test_shared_tasks_flags.py \
  --cov=app.shared.tasks.flags --cov-branch \
  --cov-report=json:/tmp/claude-1000/-home-blentz-git-pyfedi/0867703a-d309-49a0-be4d-0f73d0bbda1d/scratchpad/flags_cov.json
```

**The dotted form is required.** `--cov=app/shared/tasks/flags.py` collects
nothing, writes no JSON, and exits 0.

Read `missing_lines` and `num_partial_branches` from the JSON. Note the report's
mtime and confirm it postdates this run.

- [ ] **Step 2: Close any residual**

For each missing line or partial arm, write the test that reaches it, using the
helpers from Task 1. If a line is genuinely unreachable, do not write a test
that pretends otherwise — document the proof in the module docstring and say
why. An unreachable-claim without a proof is a gap, not a closure.

- [ ] **Step 3: Mutate — one at a time**

Run these five, each dry-run first, each restored before the next:

| # | Line | Mutation | Must be killed by |
|---|---|---|---|
| M1 | `:57` | drop the `community.private or` conjunct | `test_a_private_community_sends_no_flag` |
| M2 | `:57` | drop the `community.local_only or` conjunct | `test_a_local_only_community_sends_no_flag` |
| M3 | `:57` | drop the `not community.instance.online()` conjunct | `test_an_offline_community_instance_sends_no_flag` |
| M4 | `:75` | change `is not None` to `is None` | the skip and continue tests |
| M5 | `:45` | change `.get(post_id)` to `.filter_by(id=post_id).one()` | `test_a_missing_post_raises_AttributeError_and_rolls_back` |

For each:

```bash
sed -n '<N>p' app/shared/tasks/flags.py                    # read the real line
sed '<N>s/<pattern>/<replacement>/' app/shared/tasks/flags.py | sed -n '<N>p'   # DRY RUN, read it
sed -i '<N>s/<pattern>/<replacement>/' app/shared/tasks/flags.py
./run_tests.sh tests/test_shared_tasks_flags.py -q         # must FAIL, and name the expected test
git checkout -- app/
git diff -- app/                                            # must be empty
wc -l app/shared/tasks/flags.py                             # must be 78
```

**A mutation that applies cleanly and changes nothing is a defective mutation,
not a surviving one.** If the suite still passes, first check the dry-run
output actually differed from the original line. Three defective `sed`s were
written across sub-projects 21-22; dry-running every one is why sub-project 23
caught its no-op before running it.

- [ ] **Step 4: Add the floor**

Add to `coverage_floors.ini` under `[floors]`, at whatever percentage was
achieved (floors only ever rise):

```ini
app/shared/tasks/flags.py = 100
```

- [ ] **Step 5: Prove the floor bites**

At 100 there is no headroom to raise a scratch floor, so **invert the
experiment**: hold the floors file fixed, regress the coverage report (edit a
copy of the JSON to drop `flags.py`'s percentage below the floor), and confirm
`tests/check_coverage_floors.py` exits non-zero naming that module. Include an
**isolation control**: an unrelated module's regression in the same copied
report must not be attributed to `flags.py`. Delete the copied report
afterwards; it is scratch.

- [ ] **Step 6: Commit**

Message subject: `test: close app/shared/tasks/flags.py and set its floor`

---

## Task 7: Open `tests/test_shared_tasks_users.py` with the new harness

**Files:**
- Create: `tests/test_shared_tasks_users.py`
- Modify: `tests/factories.py`

**Interfaces:**
- Produces: `make_user_registration(user, answer='why', status=0)` in
  `tests/factories.py`; `_seed(ip='203.0.113.7', email='applicant@example.com')`
  returning `SimpleNamespace(instance, user, application)`;
  `_recording_client(monkeypatch, *responses)` returning
  `SimpleNamespace(posts=[], closed=[])`; `_no_sleep(monkeypatch)`;
  `_lowest_randint(monkeypatch)`. Tasks 8-12 consume all of these.

**Context:** `users.py` is the first non-federation target. Nothing from the
`flags.py` harness transfers except the general discipline. Three controls are
mandatory, all patched in `app.shared.tasks.users` — the module's own namespace.

**THE TRAP THAT MAKES EVERY TEST IN THIS FILE VACUOUS IF MISSED.**
`make_user` (`tests/factories.py:39`) sets `email` but leaves `ip_address` as
`None`. `users.py:36` inserts `application.user.ip_address` into `ip_list`, and
`:39`'s `','.join(ip_list)` then raises `TypeError` on the None — **inside the
`try` at `:27`**, so `:75` catches it, logs, and `continue`s. `num_banned` stays
0, no warning is written, and a test asserting "no warning" passes for entirely
the wrong reason. **`_seed` must set `ip_address` explicitly**, and Step 5 below
proves it did.

- [ ] **Step 1: Add the factory**

Append to `tests/factories.py`, following the file's existing style:

```python
def make_user_registration(user: User, answer: str = 'why', status: int = 0) -> UserRegistration:
    """A pending application. `warning` is left None -- it is the column
    app/shared/tasks/users.py writes, so a test that asserts on it needs it
    to start empty.
    """
    application = UserRegistration(user_id=user.id, answer=answer, status=status)
    db.session.add(application)
    db.session.commit()
    return application
```

Add `UserRegistration` to the `app.models` import list at the top of
`tests/factories.py` if it is not already there — check before editing.

- [ ] **Step 2: Write the module docstring and imports**

```python
"""`check_user_application` -- the cross-instance ban check.

`app/shared/tasks/users.py:14-87`, one function, 58 statements and 20 branches.
THIS IS THE FIRST TARGET IN THE CAMPAIGN THAT IS NOT FEDERATION-SHAPED, and
nothing from the `app/shared/tasks/` Flag and Announce harnesses transfers. It
makes outbound HTTP with `httpx_client`, sleeps between requests, and randomises
both the payloads and the position of the real value inside them.

THREE CONTROLS, ALL PATCHED IN `app.shared.tasks.users`:

1. `sleep` -- imported BY VALUE at `:1` (`from time import sleep`), so the
   module holds its own reference and patching `time.sleep` would miss it.
   `:50` sleeps `random.randint(1, 30)` seconds PER DOMAIN. Unpatched, this
   file alone would dominate the suite's runtime.
2. `random.randint` -- stubbed to return its lower bound, which makes
   `ip_index` and `email_index` 0, fixes the fake octets, and makes `:50`
   sleep 1 (still patched away by control 1).
3. `httpx_client` -- replaced by a RECORDING DOUBLE, not respx.

WHY A DOUBLE AND NOT respx. One of this sub-project's three production changes
is that `email_response` is never closed, against `ip_response.close()` at
`:48`. respx CANNOT observe that: the `Response` never leaves the function, and
a respx-mocked response may already report `is_closed == True` before `close()`
is called -- an assertion on `is_closed` would pass identically before and after
the fix. That is the unfailable-assertion shape fact 132 exists to catch. The
double's `close()` appends to a list instead, so the assertion fails with one
entry and passes with two.

TWO SESSIONS ARE LIVE IN THIS FUNCTION AT ONCE. `users.py` never calls
`patch_db_session` -- it is one of D314's 21 unpatched task functions -- so
`application` is read through `get_task_session()` (`:15`, `:17`) while
`get_setting` reads through the global `db.session` (app/utils.py:204). Fixture
rows must be COMMITTED before the task runs, or the task session will not see
them.

`get_setting` IS NOT A CACHING TRAP. It is decorated `@cache.memoize` at
app/utils.py:202, which invites a defence against stale values across tests.
`.env.test` sets `CACHE_TYPE=NullCache`, so the memoization is inert under test
and every call queries the database. No defence is needed; do not build one.
"""

from types import SimpleNamespace

import pytest

from app import db
from app.models import UserRegistration
from app.shared.tasks.users import check_user_application
from app.utils import set_setting
from tests.factories import make_instance, make_user, make_user_registration

APPLICANT_IP = '203.0.113.7'
APPLICANT_EMAIL = 'applicant@example.com'
```

- [ ] **Step 3: Write `_seed` and the three controls**

```python
def _seed(ip=APPLICANT_IP, email=APPLICANT_EMAIL):
    """instance, user, application -- committed.

    `ip_address` AND `email` ARE SET HERE AND MUST STAY SET.
    `make_user` (tests/factories.py:39) sets `email` but leaves `ip_address`
    None, and `:36` inserts it into a list that `:39` passes to `','.join`.
    A None member raises TypeError INSIDE the `try` at `:27`, which `:75`
    catches, logs and continues past -- so `num_banned` stays 0, no warning is
    written, and a test asserting "no warning" PASSES FOR THE WRONG REASON.
    `test_the_seed_supplies_an_ip_address` below exists to make that
    regression loud.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'applicant', local=True)
    user.ip_address = ip
    user.email = email
    db.session.commit()
    application = make_user_registration(user)
    return SimpleNamespace(instance=instance, user=user, application=application)


def _no_sleep(monkeypatch):
    """Suppress `:50`'s sleep. `users.py:1` does `from time import sleep`, so
    the name lives in the users module and patching `time.sleep` would miss
    it. Unpatched, `:50` sleeps up to 30 seconds PER DOMAIN.
    """
    monkeypatch.setattr('app.shared.tasks.users.sleep', lambda _seconds: None)


def _lowest_randint(monkeypatch):
    """Make `random.randint(a, b)` return `a`.

    Fixes `ip_index` (`:34`) and `email_index` (`:60`) at 0, so the real value
    is inserted at the FRONT of each list and the response index the code
    reads is 0. Also fixes the fake octets and makes `:50`'s argument 1.

    `random` is imported as a module at `:5`, so this patches the shared
    module for the test's duration; `monkeypatch` reverts it.
    """
    import random
    monkeypatch.setattr(random, 'randint', lambda a, _b: a)


class _Response:
    """A scripted stand-in for `httpx.Response` recording its own close.

    Only the three members `check_user_application` touches are provided:
    `status_code` (`:43`, `:69`), `json()` (`:44`, `:70`) and `close()`
    (`:48`, and -- after production change 3 -- the email leg).
    """

    def __init__(self, status_code, payload, closed):
        self.status_code = status_code
        self._payload = payload
        self._closed = closed

    def json(self):
        return self._payload

    def close(self):
        self._closed.append(self)


def _recording_client(monkeypatch, *responses):
    """Replace `httpx_client` in the users module with a recording double.

    `responses` are handed out in call order, one per `post()`. Each is a
    `(status_code, payload)` pair. Running out is an error rather than a
    silent reuse -- a test that makes more requests than it scripted is a
    test whose author lost track of the call sequence.

    Returns `SimpleNamespace(posts=[], closed=[])`:
      `posts`   -- one `(url, data)` per request, in call order.
      `closed`  -- the `_Response` objects on which `close()` was called.
                   THIS LIST IS THE SUBJECT of the email-close test.
    """
    record = SimpleNamespace(posts=[], closed=[])
    scripted = list(responses)

    def post(url, data=None, timeout=None):
        record.posts.append((url, data))
        if not scripted:
            raise AssertionError(
                f'the double was scripted for {len(responses)} responses '
                f'and received request {len(record.posts)} to {url}')
        status_code, payload = scripted.pop(0)
        return _Response(status_code, payload, record.closed)

    monkeypatch.setattr('app.shared.tasks.users.httpx_client',
                        SimpleNamespace(post=post))
    return record
```

- [ ] **Step 4: Write the guard tests at `:18` and the empty-setting path**

```python
def test_an_absent_application_returns_without_requests(db_session, monkeypatch):
    """`:18`'s first disjunct. `.get()` at `:17` returns None for an absent id.

    The double is scripted for ZERO responses, so any request at all raises
    AssertionError from `_recording_client` rather than passing silently.
    """
    s = _seed()
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch)

    check_user_application(s.application.id + 1000)

    assert client.posts == []


def test_an_application_without_a_user_returns_without_requests(
        db_session, monkeypatch):
    """`:18`'s second disjunct: the row exists but `application.user` is None.

    `UserRegistration.user` is a relationship on `user_id`
    (app/models.py:3641), so clearing the FK empties it.
    """
    s = _seed()
    s.application.user_id = None
    db.session.commit()
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch)

    check_user_application(s.application.id)

    assert client.posts == []


def test_no_ban_check_servers_configured_makes_no_requests(
        db_session, monkeypatch):
    """`:23`'s loop body never runs. `get_setting('ban_check_servers', '')`
    returns '' by default, and `''.split('\\n')` is `['']`, whose single
    member is blank -- so `:24` sends it straight to `:25`'s continue.

    THE LOOP RUNS ONCE HERE, NOT ZERO TIMES. That is worth stating because a
    reader expecting zero iterations would take the next test to be redundant.
    """
    s = _seed()
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch)

    check_user_application(s.application.id)

    assert client.posts == []


def test_the_seed_supplies_an_ip_address(db_session):
    """A guard on the fixture, not on the code.

    If `_seed` stops setting `ip_address`, `:39`'s `','.join` raises TypeError
    inside `:27`'s try, `:75` swallows it, and several tests in this file go
    green while proving nothing. This test makes that regression loud and
    names the reason.
    """
    s = _seed()

    assert s.user.ip_address == APPLICANT_IP
    assert s.user.email == APPLICANT_EMAIL
```

- [ ] **Step 5: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_users.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 6: Probe that the client double is load-bearing (fact 132)**

`httpx_client` is imported by value at `users.py:8`, so patching the string
`'app.shared.tasks.users.httpx_client'` should intercept — but **prove it**.
Temporarily script `_recording_client` so `post` raises
`RuntimeError('probe')`, write a throwaway test that configures one domain via
`set_setting`, and confirm it fails with that error. If it passes, the patch is
not intercepting and every HTTP assertion in this file is worthless. Delete the
throwaway test before committing.

- [ ] **Step 7: Commit**

```bash
git add tests/test_shared_tasks_users.py tests/factories.py
git commit -F <message-file>
```
Message subject: `test: open tests/test_shared_tasks_users.py with the ban-check harness`

---

## Task 8: The domain loop and the IP leg

**Files:**
- Modify: `tests/test_shared_tasks_users.py`

**Interfaces:**
- Consumes: `_seed`, `_no_sleep`, `_lowest_randint`, `_recording_client` from Task 7.

**Context:** `:23-25` iterate the configured domains and skip blanks. `:29-48`
are the IP leg: build three fakes, insert the real one at `ip_index`, post,
and count a ban if the response's element at that index is truthy. With
`_lowest_randint`, `ip_index` is 0.

- [ ] **Step 1: Write the loop-shape tests**

```python
def test_a_blank_line_in_the_setting_is_skipped(db_session, monkeypatch):
    """`:24`'s `if not domain.strip()` reaching `:25`'s continue, with a real
    domain after it proving the loop CONTINUES rather than aborting.

    One response is scripted for the IP leg and one for the email leg of the
    single real domain. A second domain's worth of requests would exhaust the
    script and raise.
    """
    s = _seed()
    set_setting('ban_check_servers', '\n   \nreal.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))

    check_user_application(s.application.id)

    assert [url for url, _data in client.posts] == [
        'https://real.example/api/is_ip_banned',
        'https://real.example/api/is_email_banned',
    ]


def test_the_real_ip_is_hidden_among_three_fakes(db_session, monkeypatch):
    """`:29-36`. Three fake IPs are generated and the real one INSERTED at
    `ip_index`, so the request carries four addresses and the server cannot
    tell which is under test.

    With `_lowest_randint` the index is 0, so the real address is first.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))

    check_user_application(s.application.id)

    _url, data = client.posts[0]
    submitted = data['ip_addresses'].split(',')
    assert len(submitted) == 4
    assert submitted[0] == APPLICANT_IP
```

- [ ] **Step 2: Write the four IP-leg outcome tests**

```python
def test_a_banned_ip_at_the_real_index_counts(db_session, monkeypatch):
    """`:43`, `:46` and `:47` all taken: status 200, results truthy and long
    enough, and the element at `ip_index` true.

    THE ASSERTION IS THE WARNING TEXT, not merely that a warning exists. The
    text names the count, so it distinguishes one ban from two -- which is
    what separates this test from `test_both_legs_banned_counts_twice`. The
    email leg is scripted clean here, so the 1 can only have come from the IP
    leg.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [True]), (200, [False]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(
        s.application.id).warning == '1 instances have banned this account.'


def test_a_non_200_ip_response_counts_nothing(db_session, monkeypatch):
    """`:43`'s false arm. A 500 skips the whole result block."""
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (500, None), (200, [False]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(s.application.id).warning is None


def test_an_empty_ip_result_list_counts_nothing(db_session, monkeypatch):
    """`:46`'s `if ip_results` guard, and its `len(ip_results) > ip_index`
    conjunct: an empty list is falsy AND too short. Both fail together here,
    which is why the next test exists to separate them.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, []), (200, [False]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(s.application.id).warning is None


def test_a_false_result_at_the_real_index_counts_nothing(db_session, monkeypatch):
    """`:46`'s last conjunct alone: the list is truthy and long enough, but
    the element at `ip_index` is False.

    SEPARATES the third conjunct from the first two, which the empty-list test
    above fails simultaneously.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [False, True, True, True]), (200, [False]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(s.application.id).warning is None
```

- [ ] **Step 3: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_users.py -v`
Expected: PASS, 10 tests.

**If `test_a_banned_ip_at_the_real_index_counts` fails with
`TypeError: text() takes 1 positional argument but 2 were given`, that is
D319 and it is EXPECTED at this point.** It is fixed in Task 10. Mark this
test with `@pytest.mark.xfail(strict=True, reason='D319, fixed in Task 10')`
and remove the marker in Task 10 — do not fix the production code here, and do
not weaken the assertion to make it pass.

- [ ] **Step 4: Commit**

Message subject: `test: cover the ban check's domain loop and IP leg`

---

## Task 9: The email leg and the per-domain exception handler

**Files:**
- Modify: `tests/test_shared_tasks_users.py`

**Interfaces:**
- Consumes: everything from Task 7.

**Context:** `:53-73` mirror the IP leg for email. `:75-77` catch anything
raised inside one domain's body, log it, and **continue** to the next domain.

- [ ] **Step 1: Write the email-leg tests**

```python
def test_the_real_email_is_hidden_among_three_fakes(db_session, monkeypatch):
    """`:53-62`. Three fake addresses, the real one inserted at
    `email_index` -- 0 under `_lowest_randint`."""
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))

    check_user_application(s.application.id)

    _url, data = client.posts[1]
    submitted = data['emails'].split(',')
    assert len(submitted) == 4
    assert submitted[0] == APPLICANT_EMAIL


def test_a_banned_email_counts(db_session, monkeypatch):
    """`:69`, `:72` and `:73` taken on the email leg, with the IP leg clean --
    so the count of 1 in the warning text can only have come from email."""
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [False]), (200, [True]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(
        s.application.id).warning == '1 instances have banned this account.'


def test_a_non_200_email_response_counts_nothing(db_session, monkeypatch):
    """`:69`'s false arm."""
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [False]), (500, None))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(s.application.id).warning is None


def test_an_empty_email_result_list_counts_nothing(db_session, monkeypatch):
    """`:72`'s `if email_results` guard and its length conjunct together."""
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [False]), (200, []))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(s.application.id).warning is None


def test_a_false_result_at_the_real_email_index_counts_nothing(
        db_session, monkeypatch):
    """`:72`'s last conjunct alone, separated from the first two."""
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [False]), (200, [False, True, True, True]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(s.application.id).warning is None


def test_both_legs_banned_counts_twice(db_session, monkeypatch):
    """`num_banned` accumulating across the two legs of ONE domain.

    The warning text names 2, which no single-leg test can produce -- this is
    what proves `:47` and `:73` increment the same counter.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [True]), (200, [True]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.query(UserRegistration).get(
        s.application.id).warning == '2 instances have banned this account.'
```

- [ ] **Step 2: Add the raising-payload sentinel**

`:75` is `except Exception`, so anything raised inside one domain's body is
swallowed and the loop continues. To reach that handler by a **natural** raise
rather than an injected one, give the double a payload whose `json()` fails —
the closest analogue to a decode error inside `:27`'s try.

Add next to `_Response` in Task 7's block:

```python
class _RaisingPayload:
    """A payload whose `json()` raises, standing in for a decode failure
    inside `:27`'s try. Reaching `:75`'s handler by a NATURAL raise rather
    than an injected one keeps the test on the same path a real transport or
    decode error would take."""


_RAISING_PAYLOAD = _RaisingPayload()
```

and replace `_Response.json` with:

```python
    def json(self):
        if isinstance(self._payload, _RaisingPayload):
            raise ValueError('simulated decode failure')
        return self._payload
```

- [ ] **Step 3: Write the per-domain exception test**

```python
def test_a_failing_domain_does_not_stop_the_next_one(db_session, monkeypatch):
    """`:75-77`: an exception inside one domain's body is logged and the loop
    CONTINUES to the next domain.

    THE SECOND DOMAIN'S REQUESTS ARE THE OBSERVATION. A handler that logged
    and then broke out of the loop would leave `client.posts` holding only
    broken.example's single attempt, and this assertion separates the two
    behaviours. Nothing propagates out of `check_user_application` here --
    `:75` is `except Exception` and swallowing IS the behaviour under test --
    so there is no `pytest.raises` around the call.

    Note that broken.example makes ONE request and working.example makes TWO:
    the raise lands on the IP leg, so that domain's email leg never runs.
    """
    s = _seed()
    set_setting('ban_check_servers', 'broken.example\nworking.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(
        monkeypatch,
        (200, _RAISING_PAYLOAD),   # broken.example IP leg: json() raises
        (200, [False]),            # working.example IP leg
        (200, [False]),            # working.example email leg
    )

    check_user_application(s.application.id)

    assert [url for url, _data in client.posts] == [
        'https://broken.example/api/is_ip_banned',
        'https://working.example/api/is_ip_banned',
        'https://working.example/api/is_email_banned',
    ]
```

- [ ] **Step 4: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_users.py -v`
Expected: PASS, 17 tests (with the Task 8 xfail still marked).

- [ ] **Step 5: Commit**

Message subject: `test: cover the ban check's email leg and per-domain handler`

---

## Task 10: Production change 2 — D319, the `text()` params bug

**Files:**
- Modify: `app/shared/tasks/users.py:80-81`
- Modify: `tests/test_shared_tasks_users.py`

**Interfaces:**
- Consumes: everything from Task 7.

**Context:** `:80-81` passes the params dict as a **second positional argument
to `text()`** rather than the second argument to `session.execute()`. Count the
parentheses: `text(` closes after the dict. `sqlalchemy.text` takes one
positional parameter — measured in the test container at 2.0.52:
`TypeError: text() takes 1 positional argument but 2 were given`. Every run
reaching `num_banned > 0` raises, `:83` rolls back and re-raises, and the
`warning` column is never written. **The feature has never worked.**

- [ ] **Step 1: Remove the xfail and confirm the real failure**

Delete the `@pytest.mark.xfail` marker from
`test_a_banned_ip_at_the_real_index_counts` (added in Task 8).

Run: `./run_tests.sh tests/test_shared_tasks_users.py -v`
Expected: FAIL. **Read the failure text and confirm it is exactly
`TypeError: text() takes 1 positional argument but 2 were given`.** If it is a
different error — a missing column, a session-visibility problem, a fixture
fault — stop and fix that first. A test that fails for the wrong reason proves
nothing when it later passes.

Four tests should fail here: the two banned-IP/banned-email counters and the
both-legs counter, plus any other test whose path reaches `:79`.

- [ ] **Step 2: Write the test that names the defect**

```python
def test_the_warning_update_binds_its_parameters(db_session, monkeypatch):
    """D319. `:80-81` passed the params dict as a SECOND POSITIONAL ARGUMENT
    TO `text()` rather than as the second argument to `session.execute()`.
    `sqlalchemy.text` takes one positional parameter, so every run reaching
    `:79`'s true arm raised
    `TypeError: text() takes 1 positional argument but 2 were given`,
    `:83` rolled back and re-raised, and the `warning` column was NEVER
    WRITTEN. Measured at SQLAlchemy 2.0.52.

    THE ASSERTION IS THE PERSISTED COLUMN, read back after the task. Asserting
    that `session.execute` was CALLED would be satisfied by the broken code
    the moment the line is reached, which is the unfailable shape fact 132
    exists to catch.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [True]), (200, [True]))

    check_user_application(s.application.id)

    db.session.expire_all()
    persisted = db.session.query(UserRegistration).get(s.application.id)
    assert persisted.warning == '2 instances have banned this account.'
```

- [ ] **Step 3: Make the production change**

`app/shared/tasks/users.py:80-81`, from:

```python
            session.execute(text('UPDATE "user_registration" SET warning = :warning WHERE id = :id',
                                    {'warning': f"{num_banned} instances have banned this account.", 'id': application_id}))
```

to:

```python
            session.execute(text('UPDATE "user_registration" SET warning = :warning WHERE id = :id'),
                            {'warning': f"{num_banned} instances have banned this account.", 'id': application_id})
```

The closing paren of `text(` moves to before the dict; `session.execute(`'s
closing paren moves to the end.

- [ ] **Step 4: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_users.py -v`
Expected: PASS, 18 tests. Every previously failing counter test now passes.

- [ ] **Step 5: Check the line count**

```bash
git diff --numstat -- app/shared/tasks/users.py
wc -l app/shared/tasks/users.py    # must still be 87
```

- [ ] **Step 6: Commit**

```bash
git add app/shared/tasks/users.py tests/test_shared_tasks_users.py
git commit -F <message-file>
```
Message subject: `fix: bind the ban-check warning update's parameters`

---

## Task 11: Production change 3 — D320, close `email_response`

**Files:**
- Modify: `app/shared/tasks/users.py` (email leg)
- Modify: `tests/test_shared_tasks_users.py`

**Interfaces:**
- Consumes: `_recording_client`'s `closed` list from Task 7.

**Context:** `ip_response.close()` at `:48` has no counterpart after the email
leg's result block. This is the assertion most at risk of being born
unfailable — see Task 7's docstring for why respx cannot observe it.

- [ ] **Step 1: Write the failing test**

```python
def test_both_responses_are_closed(db_session, monkeypatch):
    """D320. `ip_response.close()` at `:48` had no counterpart on the email
    leg, so every email response was left unclosed.

    THE DOUBLE'S `closed` LIST IS THE SUBJECT, and it exists because respx
    cannot observe this: the `Response` never leaves the function, and a
    respx-mocked response may already report `is_closed == True` before
    `close()` is called -- an `is_closed` assertion would pass identically
    before and after the fix. Before the fix this list holds ONE entry; after,
    TWO.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))

    check_user_application(s.application.id)

    assert len(client.closed) == 2
```

- [ ] **Step 2: Run it and read the failure**

Run: `./run_tests.sh tests/test_shared_tasks_users.py::test_both_responses_are_closed -v`
Expected: FAIL on `assert 1 == 2`. **Confirm the number is 1, not 0.** A 0 means
the IP leg's `close()` is also not being observed, which would mean the double
is not wired the way the test assumes — fix that before fixing the production
code.

- [ ] **Step 3: Make the production change**

In `app/shared/tasks/users.py`, after the email result block (the `if
email_response.status_code == 200:` block ending at `:73`) and still inside
`:27`'s `try`, at the same indentation as `:48`'s `ip_response.close()`, add:

```python
                email_response.close()
```

**Re-derive the line number and the indentation from the current tree** — the
file is 87 lines and this insertion makes it 88. Do not assume `:74`.

- [ ] **Step 4: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_users.py -v`
Expected: PASS, 19 tests.

- [ ] **Step 5: Record the new line count**

```bash
git diff --numstat -- app/shared/tasks/users.py
wc -l app/shared/tasks/users.py    # now 88, NOT 87
```

**Every later mutation restore in Task 12 must assert 88.** Sub-project 22
recorded that `wc -l` under-reports a file with no trailing newline; check that
this file ends with one.

- [ ] **Step 6: Commit**

Message subject: `fix: close the ban check's email response`

---

## Task 12: Close `users.py`, mutate, and set its floor

**Files:**
- Modify: `tests/test_shared_tasks_users.py` (only if coverage shows gaps)
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Cover the outer handler at `:83-87`**

```python
def test_a_database_failure_rolls_back_and_re_raises(db_session, monkeypatch):
    """`:83-85`'s except arm and `:86-87`'s finally, reached WITHOUT a faked
    exception in the task body.

    `get_task_session` is replaced by one whose `execute` raises, which is the
    nearest natural analogue to the database rejecting the UPDATE. The
    recorded ORDER fixes `finally` running after `except`, which a
    `raises`-only test cannot observe.

    THE PATCH TARGET IS THE USERS MODULE. `app/shared/tasks/users.py:10`
    imports `get_task_session` into the users namespace and `:15` resolves it
    there; patching `app.utils.get_task_session` would apply cleanly and
    observe nothing.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.users as users_module

    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [True]), (200, [True]))

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
            raise RuntimeError('database refused the update')

        session.rollback = rollback
        session.close = close
        session.execute = execute
        return session

    monkeypatch.setattr(users_module, 'get_task_session', _make)

    with pytest.raises(RuntimeError):
        check_user_application(s.application.id)

    assert calls == ['rollback', 'close']


def test_the_session_is_closed_on_the_happy_path(db_session, monkeypatch):
    """`:86-87`'s finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the test above: without it, `finally` running is only ever
    observed alongside an exception, and a handler that closed only in the
    `except` arm would pass every other test in this file.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.users as users_module

    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [False]), (200, [False]))

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

        session.rollback = rollback
        session.close = close
        return session

    monkeypatch.setattr(users_module, 'get_task_session', _make)

    check_user_application(s.application.id)

    assert calls == ['close']
```

- [ ] **Step 2: Measure**

```bash
./run_tests.sh tests/test_shared_tasks_users.py \
  --cov=app.shared.tasks.users --cov-branch \
  --cov-report=json:/tmp/claude-1000/-home-blentz-git-pyfedi/0867703a-d309-49a0-be4d-0f73d0bbda1d/scratchpad/users_cov.json
```

Dotted form required. Read `missing_lines` and `num_partial_branches`.

- [ ] **Step 3: Close any residual**

Write the test that reaches each missing line or partial arm. For anything
genuinely unreachable, document the proof rather than writing a test that
pretends otherwise.

- [ ] **Step 4: Mutate — one at a time**

| # | Line | Mutation | Must be killed by |
|---|---|---|---|
| M6 | `:18` | drop `or not application.user` | `test_an_application_without_a_user_returns_without_requests` |
| M7 | `:24` | change `if not domain.strip()` to `if False` | `test_a_blank_line_in_the_setting_is_skipped` |
| M8 | `:43` | change `== 200` to `!= 200` | the IP-leg 200 and non-200 tests |
| M9 | `:79` | change `> 0` to `>= 0` | the three "counts nothing" tests |
| M10 | email close | delete the line added in Task 11 | `test_both_responses_are_closed` |
| M11 | `:80-81` | restore the broken `text()` shape | `test_the_warning_update_binds_its_parameters` |

Same instrument as Task 6: dry-run without `-i` and read the produced line,
apply, run, restore with `git checkout -- app/`, then assert an empty
`git diff -- app/` and `wc -l app/shared/tasks/users.py` **== 88**.

M11 is the important one: it restores a defect the campaign just fixed and
confirms a named test catches it. If M11 survives, the D319 test is not
actually discriminating and Task 10's work is unproven.

- [ ] **Step 5: Add the floor**

```ini
app/shared/tasks/users.py = 100
```

- [ ] **Step 6: Prove the floor bites**

Same inversion as Task 6: hold the floors fixed, regress a copy of the report
below the floor, confirm a non-zero exit naming `users.py`, and include an
isolation control so an unrelated module's regression is not misattributed.
Delete the copied report.

- [ ] **Step 7: Commit**

Message subject: `test: close app/shared/tasks/users.py and set its floor`

---

## Task 13: Register the findings and the harness facts

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

**Context:** Next free number is **D319**, confirmed at `896150f1`; a `D322`
sighting in `docs/` is base64 inside an SVG, not an allocation. Facts end at
**132**; append from **133**.

- [ ] **Step 1: Add the three new register entries**

- **D319** — `app/shared/tasks/users.py:80-81`. The params dict was a second
  positional argument to `text()` rather than the second argument to
  `session.execute()`. `sqlalchemy.text` takes one positional parameter, so
  every run reaching `num_banned > 0` raised
  `TypeError: text() takes 1 positional argument but 2 were given`, `:83`
  rolled back and re-raised, and the `warning` column was never written.
  Measured at SQLAlchemy 2.0.52. **Fixed in this sub-project.** Record that it
  was found by coverage rather than by a bug report, and that the module sat at
  12.82% — that is the entry's whole point.
- **D320** — `app/shared/tasks/users.py`, `email_response` never closed against
  `ip_response.close()` at `:48`. **Fixed in this sub-project.**
- **D321** — `app/shared/tasks/flags.py:73` runs a raw
  `session.query(Instance).filter(Instance.id.in_(instance_ids))` instead of
  going through `community.following_instances()` (`app/models.py:842-851`), so
  it lacks the `Instance.dormant == False` filter at `:849` and
  `Instance.gone_forever == False` at `:850` that every other sender in the
  package inherits. Reports are therefore delivered to dormant and
  permanently-gone instances. **Not fixed.** Record it as the mirror image of
  D302 — D302 is the *redundant* `online()` check layered on a filter that
  already excludes those rows; D321 is that filter's **absence**, where the
  redundant check would have been harmless. Same fact from opposite ends.
  **Record the consequence at its measured strength**: this is a mechanism, and
  what `send_post_request` does against a dead host was not investigated here.
  Do not promote it to a crash it has not been shown to cause.

- [ ] **Step 2: Make the in-place edits**

- **D309** — drops from six omitting files to **five**, `flags.py:57` closed.
  **State the counting unit in the number you write.** The cell counts by file,
  not by line: `deletes.py` contributes two guards (`:127`, `:130`) and counts
  once, which is why "six sites" and "seven lines" are both correct. Re-derive
  the remaining list against the tree before writing it.
- **D314** — extend the cell with `users.py`'s carrier, measured: `application`
  read through `get_task_session()` (`:15`, `:17`) while `get_setting` reads
  through the global `db.session` (`app/utils.py:204`), both live in one
  function. **Mechanism measured, no consequence claimed** — the same discipline
  D311 and D312's cells state.
- **The two lookup styles at `flags.py:30`/`:45`** — read the register and find
  out whether **D316 or D317 already carries this shape**. If one does, extend
  it in place with this fourth module rather than allocating a new number. The
  campaign's copy-hunt rule applies to register cells as much as to defects:
  enumerate, or the next reader invents a number.
- **`_recording_task_session` now exists in three copies**
  (`test_shared_tasks_send_reply.py:1637`,
  `test_shared_tasks_send_answer.py:567`, `test_shared_tasks_flags.py`).
  Record as a test-side duplication finding; do not fix.
- Note that sub-project 23's parked **Ruling 3** citation
  (`signature.py:102` for an add at `:105`) was **corrected on the way into**
  `test_shared_tasks_flags.py` rather than propagated, and that the original
  in `test_shared_tasks_add_remove.py:179` still stands.

- [ ] **Step 3: Append the three harness facts to `tests/README.md`**

- **133** — `.env.test` sets `CACHE_TYPE=NullCache`, so every `@cache.memoize`
  is inert under test and the decorated function queries the database on every
  call. `get_setting` (`app/utils.py:202-211`) is the case that prompts this.
  **Do not design defences against stale memoized values**; there is no cache
  to go stale. Recording this because the decorator is visible and the config is
  not, so the defence looks obviously necessary right up until it is measured.
- **134** — `respx` cannot observe `Response.close()`. The response never
  leaves the function under test, and a mocked response may already report
  `is_closed == True` before `close()` is called, so an `is_closed` assertion
  passes identically before and after a fix. Proving a close requires replacing
  the client with a recording double whose `close()` appends to a list. Carrier:
  `app/shared/tasks/users.py`'s email leg, D320.
- **135** — a name imported by value (`from time import sleep`) must be patched
  in the **importing** module's namespace. `app/shared/tasks/users.py:1` does
  exactly this and `:50` sleeps `random.randint(1, 30)` seconds per configured
  domain; patching `time.sleep` misses it entirely and the suite absorbs the
  wait. Same family as fact 132's class-vs-instance question, and settled by the
  same probe: make the stub raise and confirm the test fails.

- [ ] **Step 4: Verify every citation in what you wrote**

For each `file:line` you added to either document, print the line and confirm
it carries the claimed statement — not merely that it resolves. Sub-project 23's
Ruling 3 exists because every sweep the campaign runs checks resolution and not
semantics, and a number that resolves to the wrong statement passes all of them.

- [ ] **Step 5: Commit**

Message subject: `docs: register sub-project 24's findings and harness facts`

---

## Final verification (controller only)

- [ ] Full suite, **foreground and unpiped**:

```bash
./run_tests.sh -q > <scratchpad>/final24.log 2>&1
echo "REAL PYTEST EXIT=$?"
tail -1 <scratchpad>/final24.log
```

- [ ] Collect the test counts from **collection**, not grep:

```bash
./run_tests.sh tests/test_shared_tasks_flags.py --collect-only -q | tail -2
./run_tests.sh tests/test_shared_tasks_users.py --collect-only -q | tail -2
```

- [ ] Confirm the production surface is exactly what was intended:

```bash
git diff --numstat <base>..HEAD -- app/
wc -l app/shared/tasks/flags.py app/shared/tasks/users.py   # 78 and 88
```

- [ ] `python3 tests/check_coverage_floors.py <report> coverage_floors.ini` —
  all floors met, and **read the report mtime it prints**. A `-q` run writes no
  coverage, so a floors check after one re-reads the previous report and is not
  new evidence.

- [ ] `git status --short` — only `?? claude_test`, which is not this
  campaign's and must not be touched.

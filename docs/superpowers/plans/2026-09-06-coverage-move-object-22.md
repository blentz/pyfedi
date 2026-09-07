# Sub-project 22: closing `app/shared/tasks/pages.py` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover `move_object`, `move_post` and the `make_post`/`edit_post` wrappers in `app/shared/tasks/pages.py`, landing two production changes, taking the module from 84.91% to a full close.

**Architecture:** All tests append to the existing `tests/test_shared_tasks_send_post.py` (2334 lines, **64 collected tests** — `grep -c '^def test_'` undercounts by 3 parametrised expansions; always use the collected count), which already carries the prelude they need. New helpers are appended at the **end** of that file rather than beside their siblings, because 11 committed citations point into it and a mid-file insertion would shift them. Assertions are on **serialized outbound request bytes** via respx, never on in-memory dicts.

**Tech Stack:** pytest, respx/httpx, SQLAlchemy, Flask, Celery (eager), podman-compose via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-06-coverage-move-object-22-design.md`

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time, and **in the foreground**.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it** (`pytest.ini:26-27`). The campaign's old "still exits 0" claim was measured false; the observed exit 0 was a shell **pipeline** eating the status. Read `${PIPESTATUS[0]}`, or do not pipe. Continue to check the test count and the coverage report's mtime, which catch more than a timeout.
- **A wrong `--cov` target fails silently and green** (fact 117): `--cov` takes a module path, so a file path collects nothing, writes no JSON, and exits 0.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- **A wedged podman stack reports failures that are not regressions.** Run `./run_tests.sh --down` and retry before believing any failure.
- **Mutation instrument.** One mutation at a time, applied as a targeted single-line `sed`, never a whole-file rewrite. **Dry-run every substitution without `-i` and confirm the produced line before trusting it** — sub-project 21 recorded a mutation that applied cleanly and changed nothing, and a nine-test kill that mislabelled which conjunct it negated. A mutation's kill count says nothing about whether the mutation was the one you meant. `pages.py` is **435 lines** before and after both production changes; assert the line count and an empty `git diff -- app/` after every apply and restore.
- **Neither production change may shift an existing line number.** Both edits to `pages.py` extend existing lines, and `TaskError` is appended after `app/utils.py`'s last line. After the changes, `wc -l app/shared/tasks/pages.py` is still 435, and every pre-existing `utils.py:NNN` citation still resolves — only `app/utils.py`'s own line count grows, from the end.
- Mutation record: the mutation, the test that killed it, **assertion-kill or crash-kill**, **sole or multi**. A mutant that applies but does not change the program is a **no-op substitution** — not a survivor, and not an equivalent mutant.
- **Commit the production changes before running the mutations**: the clean-tree assertion is impossible while a fix is uncommitted, because `git checkout -- app/` would discard it.
- **Use `ast.parse` and `FunctionDef.end_lineno` for extents, never a convention.**
- Every line number must be re-derived against the current tree before it is written down (fact 99). Citation sweeps run in two passes, `file:line` then bare paths against `git ls-files` (fact 100), and **read the first pass's output to the end**.
- **When a file shifts, sweep by the cause — the moved file — not by the topic that made you notice.**
- **Fix a citation's symbol, not just its number**, and the converse: a corrected symbol beside a stale number is still a wrong citation, and the correction makes the stale number look freshly checked.
- Enumerate conditional expressions by AST walk, not grep (fact 94) — coverage.py emits no arc for one (fact 87).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.

---

## File Structure

| file | responsibility |
|---|---|
| `tests/test_shared_tasks_send_post.py` (modify) | all new helpers and tests, **appended at the end**; the import at `:71` widened in place |
| `app/shared/tasks/pages.py:398` (modify) | D309's `private` conjunct — extends an existing line |
| `app/shared/tasks/pages.py:396` (modify) | raises `TaskError` — extends an existing line |
| `app/utils.py` (modify) | `TaskError` appended after the last line |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | D314 onward; D302 extended |
| `tests/README.md` (modify) | harness facts from 124 |
| `coverage_floors.ini` (modify) | raise `app/shared/tasks/pages.py` from 84 |

**Everything new goes at the end of the test file.** 11 committed citations point into it; a mid-file insertion shifts them and invalidates all below the insertion point.

---

### Task 1: Prelude additions and a smoke test

**Files:**
- Modify: `tests/test_shared_tasks_send_post.py`

**Interfaces:**
- Produces: `_make_deliverable(s, inbox=PEER_INBOX)`; `_recording_task_session(monkeypatch)` returning `SimpleNamespace(calls=[])`; `_move(s, target=None)`.

- [ ] **Step 1: Widen the existing import line**

`tests/test_shared_tasks_send_post.py:71` currently reads `from app.shared.tasks.pages import send_post`. Re-derive that line number against the current tree, then widen it **in place**:

```python
from app.shared.tasks.pages import edit_post, make_post as make_post_task, move_object, move_post, send_post
```

**`make_post` MUST be aliased.** `from tests.factories import (..., make_post, ...)` runs two lines below and would rebind the bare name, so a later `make_post(...)` call would reach the FACTORY, not the Celery wrapper — silently, with a confusing failure. `edit_post`, `move_object`, `move_post` and `send_post` do not collide.

All five names are added **now**, in one edit, even though `make_post` and `edit_post` are not used until Task 7 — so no later task has to touch this line again. Do not add a second import line: that would shift the 11 citations pointing into this file.

- [ ] **Step 2: Append the three helpers at the END of the file**

```python
def _make_deliverable(s, inbox=PEER_INBOX):
    """Attach the community to a real peer Instance and give it an inbox.

    The half of `_remote_inbox` that touches the database and nothing else.
    The early-return tests call this ALONE, because they assert the delivery
    never happens and `http_mock` is built with `assert_all_called=True` --
    registering a route they expect never to fire would fail them for the
    wrong reason.

    Defined here at the end of the file rather than beside `_remote_inbox`
    because 11 committed citations point into this file and a mid-file
    insertion would shift every one below it.
    """
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()


def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    Ported from tests/test_shared_tasks_send_reply.py -- copied rather than
    imported across test modules, which is this campaign's deliberate pattern.

    The session is real and does real work; only the observation is added, by
    wrapping the two methods rather than replacing the object. A fake session
    would prove the wrapper calls methods on a mock; this proves it calls them
    on the session the function actually used.

    THE PATCH TARGET IS THE `pages` MODULE. `app/shared/tasks/pages.py`
    imports `get_task_session` into its own namespace, so patching
    `app.utils.get_task_session` would miss the binding `make_post`,
    `edit_post` and `move_post` actually call -- and the tests would pass
    while observing nothing.

    Returns a `SimpleNamespace(calls=[])`; the wrapper appends 'rollback' and
    'close' in the order they happened, so `finally` running after `except` is
    observable rather than assumed.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.pages as pages_module

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

    monkeypatch.setattr(pages_module, 'get_task_session', _make)
    return record


def _move(s, target=None):
    """`move_object(session, user_id, object, origin, target)`.

    `origin` is always the seeded community; `target` defaults to a SECOND
    community so the two differ, which is what a real move means. Both must be
    `Community` instances or `:393`'s guard raises.
    """
    if target is None:
        target = make_community('c2')
        db.session.commit()
    return move_object(db.session, s.user.id, s.post, origin=s.community,
                       target=target)
```

- [ ] **Step 3: Append a smoke test proving the harness delivers**

```python
def test_a_remote_community_receives_the_bare_move(db_session, http_mock):
    """The harness itself: `:416`'s FALSE arm reaches `:435` and the bytes are
    readable. Every later move test depends on this working."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _move(s)

    assert route.called
    assert _sent_activity(route)['type'] == 'Move'
```

- [ ] **Step 4: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: **65** passed (64 existing + 1 new).

- [ ] **Step 5: Commit**

Subject: `test: add the move_object harness helpers to the send_post file`

---

### Task 2: The `:398` guard — D309's third site

**Files:**
- Modify: `app/shared/tasks/pages.py:398`
- Test: `tests/test_shared_tasks_send_post.py`

**Interfaces:**
- Consumes: `_seed`, `_move`, `_make_deliverable` from Task 1.

`:398` currently reads:

```python
    if community.local_only or not community.instance.online():
```

The discriminator is `ActivityPubLog.query.count()`, not a call count: `post_request` (`app/activitypub/signature.py:103-105`) writes a row unconditionally before the transport, so 0 proves `:399` returned and 1 proves it did not. These tests call `_make_deliverable` and **not** `_remote_inbox`.

- [ ] **Step 1: Write the two arms of the existing guard**

```python
def test_a_local_only_community_does_not_federate_the_move(db_session, http_mock):
    """:398's TRUE arm via `local_only`, returning at :399."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_receive_the_move(db_session, http_mock):
    """:398's TRUE arm via `not instance.online()`.

    `Instance.online()` (app/models.py:118-119) is
    `not (self.dormant or self.gone_forever)`, so setting `dormant` on the
    community's OWN instance closes it. `_make_deliverable` reassigns
    `community.instance_id` to the peer before its commit, so this lands on
    the instance the guard actually reads.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 2: Run them**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: **67** passed.

- [ ] **Step 3: Write the failing test for the guard that does not exist yet**

```python
def test_a_private_community_does_not_federate_the_move(db_session, http_mock):
    """:398's `private` conjunct -- the first of this sub-project's two
    production changes.

    `Community.private` (app/models.py:611) is commented "only members can
    view. no federation.", and before this conjunct landed `:398` tested only
    `local_only` and `instance.online()` -- so a private, non-local-only
    community federated its moves out.

    D309's THIRD closed site of ten; sub-projects 20 and 21 closed
    `notes.py:143` and `notes.py:248`. `local_only` is left False
    deliberately: with it True the test would pass on the pre-existing
    conjunct and prove nothing.

    NEITHER SIBLING GUARD IS A TEMPLATE. `pages.py:153` is
    `local_only or private` with NO `online()` check; `notes.py:248` is
    `local_only or private or not instance.online()`. This one matches :248,
    because :398 already tests `online()`.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0
```

- [ ] **Step 4: Run it and watch it FAIL**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q -k test_a_private_community_does_not_federate_the_move`
Expected: FAIL, `assert 1 == 0` — the delivery was attempted and wrote a failure row.

**Do not proceed until you have seen this fail.** A guard that lands before its test has failed is a guard nothing proves.

- [ ] **Step 5: Land the guard as a single-line edit**

```bash
sed -n '398p' app/shared/tasks/pages.py
sed -i '398s/local_only or not/local_only or community.private or not/' app/shared/tasks/pages.py
sed -n '398p' app/shared/tasks/pages.py
wc -l app/shared/tasks/pages.py
```

Expected: the line now reads

```python
    if community.local_only or community.private or not community.instance.online():
```

and `wc -l` is still **435**.

- [ ] **Step 6: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: **68** passed.

- [ ] **Step 7: Commit**

Subject: `fix: stop move_object federating a private community's move`

---

### Task 3: `TaskError` — the `:396` raise

**Files:**
- Modify: `app/utils.py` (append at end), `app/shared/tasks/pages.py:11`, `app/shared/tasks/pages.py:396`
- Test: `tests/test_shared_tasks_send_post.py`

**Interfaces:**
- Produces: `TaskError`, importable as `from app.utils import TaskError`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_non_community_origin_raises_task_error(db_session):
    """:393's FALSE arm via `origin`, raising at :396 -- the second of this
    sub-project's two production changes.

    `move_object` requires BOTH `origin` and `target` to be `Community`. This
    test closes the guard on `origin`; its companion below closes it on
    `target`. TWO tests are needed, not one: a mutant changing `and` to `or`
    is killed by neither alone, because either single non-Community argument
    still leaves the other's isinstance True.

    Raising `TaskError` rather than a bare `Exception` is the change. A caller
    can now catch this failure without catching everything -- `move_post`'s
    handler at :383 is `except Exception:` and so is unaffected today.
    """
    s = _seed(with_keys=True)
    target = make_community('c2')
    db.session.commit()

    with pytest.raises(TaskError):
        move_object(db.session, s.user.id, s.post, origin=s.post, target=target)


def test_a_non_community_target_raises_task_error(db_session):
    """:393's FALSE arm via `target` -- the companion to the test above, and
    the half that makes an `and`->`or` mutant die."""
    s = _seed(with_keys=True)

    with pytest.raises(TaskError):
        move_object(db.session, s.user.id, s.post, origin=s.community,
                    target=s.post)
```

**The test file has no `from app.utils import` line, and you must not add one at the top.** The module's import block is at `:55-72`; a new line there shifts every one of the 11 committed citations into this file. Because all of this sub-project's code is appended at the end, put the import there too, immediately above your new tests:

```python
# Imported here rather than in the import block at the top of this file:
# 11 committed citations point into this file, and a new line at :72 would
# shift every one of them by one. Appending costs nothing.
from app.utils import TaskError
```

Re-derive `:55-72` before relying on it.

- [ ] **Step 2: Run and watch it FAIL**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q -k task_error`
Expected: FAIL at import — `ImportError: cannot import name 'TaskError'`.

- [ ] **Step 3: Append `TaskError` to the END of `app/utils.py`**

```python


class TaskError(Exception):
    """Raised by app/shared/tasks/ when a task is given arguments it cannot act on.

    DEFINED AT THE END OF THIS FILE DELIBERATELY, not beside get_task_session
    and patch_db_session where it thematically belongs. This module carries
    358 `utils.py:NNN` citations in tracked files, 135 of them at or after
    :3673; inserting there would invalidate all 135. Appending after the last
    line invalidates none. Do not "tidy" this upward without re-deriving those
    citations first.

    Introduced by sub-project 22 for app/shared/tasks/pages.py's
    `move_object`, which previously raised a bare `Exception`. `raise
    Exception(...)` is the established idiom across app/shared/ -- see the
    findings register -- so this is the first narrow raise among eleven sites
    and is intended as the migration target for the rest.
    """
```

- [ ] **Step 4: Widen `pages.py`'s existing import and change the raise**

```bash
sed -n '10,11p' app/shared/tasks/pages.py
sed -i '11s/    patch_db_session/    patch_db_session, TaskError/' app/shared/tasks/pages.py
sed -i "396s/raise Exception(/raise TaskError(/" app/shared/tasks/pages.py
sed -n '11p;396p' app/shared/tasks/pages.py
wc -l app/shared/tasks/pages.py
```

Expected: `:396` reads `raise TaskError('Unsupported origin or target')`, and `wc -l` is still **435**. If `:11` is not the continuation line, re-derive it — do not add a new import line.

- [ ] **Step 5: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: **70** passed.

- [ ] **Step 6: Verify no citation shifted**

```bash
wc -l app/shared/tasks/pages.py   # 435
git diff --stat app/utils.py       # additions only, at the end
```

- [ ] **Step 7: Commit**

Subject: `refactor: raise TaskError from move_object instead of a bare Exception`

---

### Task 4: `move_object`'s remote path

**Files:**
- Test: `tests/test_shared_tasks_send_post.py`

**Interfaces:**
- Consumes: `_seed`, `_move`, `_remote_inbox`, `_sent_activity`, `_key_id_of` from the existing prelude and Task 1.

`:416`'s false arm runs `:435`, posting the bare `Move` to `community.ap_inbox_url` signed as the **user**.

- [ ] **Step 1: Write the remote-path tests**

```python
def test_the_remote_move_carries_its_full_key_set_and_keeps_its_context(
        db_session, http_mock):
    """:416's FALSE arm, delivered by :435.

    `@context` is asserted PRESENT because nothing nests this object on this
    path -- :417's `del` runs only under `is_local()`, which is not taken
    here, so the `@context` built at :409 survives to the wire.

    `origin` and `target` are the two fields that distinguish a Move from
    every other activity this module sends, and they must differ: asserting
    both is what catches a mutant that passed the same community twice.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    target = make_community('c2')
    db.session.commit()

    _move(s, target=target)

    sent = _sent_activity(route)
    assert sent['type'] == 'Move'
    assert set(sent) == {'id', 'type', 'actor', 'object', '@context',
                         'origin', 'target', 'to', 'cc'}
    assert sent['actor'] == s.user.public_url()
    assert sent['object'] == s.post.public_url()
    assert sent['origin'] == s.community.public_url()
    assert sent['target'] == target.public_url()
    assert sent['origin'] != sent['target']
    assert sent['to'] == ['https://www.w3.org/ns/activitystreams#Public']
    assert sent['cc'] == [s.community.public_url()]


def test_the_remote_move_is_signed_as_the_user(db_session, http_mock):
    """:435 signs with `user.public_url() + '#main-key'`, where :433 signs as
    the COMMUNITY. `keyId` is the only observable that separates them, and the
    user's and community's public urls differ in path, so a mutant swapping
    the signer produces a valid but wrong keyId and this fails rather than
    merely not-noticing.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _move(s)

    assert _key_id_of(route) == s.user.public_url() + '#main-key'
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: **72** passed.

- [ ] **Step 3: Commit**

Subject: `test: cover move_object's remote arm`

---

### Task 5: `move_object`'s local path and the follower loop

**Files:**
- Test: `tests/test_shared_tasks_send_post.py`

**Interfaces:**
- Consumes: `_seed`, `_move`, `_sent_activity`, `_key_id_of`, `_community_follower` from the existing prelude and Task 1.

`:416`'s true arm deletes `move['@context']` at `:417`, builds the `Announce` at `:422-430`, and delivers per follower at `:433`. `_seed()`'s default leaves the community local. A follower is required or `:431`'s loop body never runs.

**`:432`'s `instance.online()` conjunct is NOT to be exercised as a live branch.** `Community.following_instances()` (`app/models.py:842-851`) already filters `Instance.dormant == False` at `:849` and `Instance.gone_forever == False` at `:850`, and `Instance.online()` (`app/models.py:118-119`) is exactly `not (self.dormant or self.gone_forever)` — the conjunct is unreachable-False and belongs to Task 9's register entry. Do not pass `dormant=True` expecting a skip.

- [ ] **Step 1: Write the local-path and loop tests**

```python
def test_a_local_community_announces_the_move_and_strips_its_inner_context(
        db_session, http_mock):
    """:416's TRUE arm -- :417's `del move['@context']` and the Announce built
    at :422-430.

    The Announce keeps the `@context` built at :427; the Move nested at :426
    has had its own stripped at :417. Asserting BOTH directions is what
    catches a mutant that deletes from the wrong object -- either alone would
    still accept a well-formed activity.

    `cc` is the community's followers collection here (:421), NOT the
    `[community.public_url()]` the inner Move carries (:403) -- :421 rebinds
    the name to a NEW list, so the inner object's `cc` still points at the
    old one. Asserting both proves the rebinding did not alias.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _move(s)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['actor'] == s.community.public_url()
    assert sent['object']['type'] == 'Move'
    assert '@context' not in sent['object']
    assert sent['cc'] == [s.community.ap_followers_url]
    assert sent['object']['cc'] == [s.community.public_url()]


def test_the_announced_move_is_signed_as_the_community(db_session, http_mock):
    """:433 signs with `community.private_key` and
    `community.public_url() + '#main-key'` -- the companion to Task 4's
    user-signed assertion."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _move(s)

    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'


def test_a_local_community_with_no_followers_sends_no_move(db_session, http_mock):
    """:431's loop never entered -- the arc straight past the loop.

    `following_instances()` returns empty because no CommunityMember exists on
    a remote instance. The Announce is still BUILT at :422-430; nothing
    delivers it.
    """
    s = _seed(with_keys=True)

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_following_instance_without_an_inbox_gets_no_move(db_session, http_mock):
    """:432's FALSE arm -- the loop continuing.

    `with_inbox=False` leaves `Instance.inbox` None, closing :432's FIRST
    conjunct before any of the other three is evaluated. No route is
    registered, so `http_mock`'s `assert_all_called=True` is not tripped, and
    the count of 0 rules out a request attempted against a None inbox.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, with_inbox=False)

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_one_following_instance_is_skipped_while_another_receives_the_move(
        db_session, http_mock):
    """Both loop arms in ONE run -- the skip, then the delivery.

    The discriminating case: a single-instance test cannot show that the guard
    skips an instance WITHOUT also stopping the loop, because with one member
    "skipped" and "loop ended" look identical. With two, the delivered one
    proves iteration continued past the skipped one.

    THE ORDER ASSERTION IS LOAD-BEARING AND NOT DECORATION.
    `Community.following_instances()` (app/models.py:842-851) ends in an
    unordered `.distinct().all()` -- no ORDER BY -- so the order is a property
    of Postgres's query plan, not of the code. If it ever reverses, a mutant
    turning "skip and continue" into "skip and break" would still leave the
    delivered route called once, and this test would pass while no longer
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

    _move(s)

    assert good.route.call_count == 1
    assert db.session.query(ActivityPubLog).count() == 1
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: **77** passed.

- [ ] **Step 3: Commit**

Subject: `test: cover move_object's Announce arm and follower loop`

---

### Task 6: `move_post`'s wrapper

**Files:**
- Test: `tests/test_shared_tasks_send_post.py`

**Interfaces:**
- Consumes: `_seed`, `_remote_inbox`, `_sent_activity`, `_recording_task_session`, `_make_deliverable` from the existing prelude and Task 1.

```python
@celery.task
def move_post(send_async, user_id, old_community_id, new_community_id, post_id):
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                post = session.query(Post).get(post_id)
                if post and not post.deleted:
                    ...
                    move_object(session, user_id, post, origin=old_community, target=new_community)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
```

**`:378` uses `.get()`, so a missing post returns `None` and `:379`'s guard swallows it — the `except` arm is NOT reachable that way.** It is reached by a bad **community** id: `.get()` returns `None`, `:393`'s `isinstance` fails, and `:396` raises `TaskError`.

- [ ] **Step 1: Write the wrapper tests**

```python
def test_move_post_delivers_a_move(db_session, http_mock):
    """`move_post` (:373-387) -- the happy path through :379's TRUE arm.

    `send_async` is accepted and ignored; None is passed to prove it is not
    read.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    target = make_community('c2')
    db.session.commit()

    move_post(None, s.user.id, s.community.id, target.id, s.post.id)

    assert _sent_activity(route)['type'] == 'Move'


def test_move_post_does_nothing_when_the_post_is_missing(db_session, http_mock):
    """:379's FALSE arm via `post` being None.

    `:378`'s `.get()` returns None for an absent id, and `:379`'s first
    conjunct closes. NOTHING RAISES -- the guard swallows it -- which is why
    this is a distinct case from the deleted-post test below and cannot be
    merged with it.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    target = make_community('c2')
    db.session.commit()

    move_post(None, s.user.id, s.community.id, target.id, s.post.id + 1000)

    assert db.session.query(ActivityPubLog).count() == 0


def test_move_post_does_nothing_when_the_post_is_deleted(db_session, http_mock):
    """:379's FALSE arm via `post.deleted`.

    The companion to the test above and a DIFFERENT failure mode: the post
    exists and is found, and the second conjunct closes. A single "no work
    happened" assertion could not tell these two apart, which is why they are
    separate named tests.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    target = make_community('c2')
    s.post.deleted = True
    db.session.commit()

    move_post(None, s.user.id, s.community.id, target.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_move_post_rolls_back_and_re_raises_on_a_bad_community(
        db_session, monkeypatch):
    """:383-385's except arm and :386-387's finally, reached by a NATURAL
    raise.

    A community id with no row makes `:380`'s `.get()` return None, so
    `:393`'s `isinstance(target, Community)` is False and `:396` raises
    `TaskError`. Nothing is faked: the exception is the one the real path
    produces, so a refactor that stopped raising would fail this test rather
    than leave it green.

    NOTE THE ASYMMETRY WITH THE MISSING-POST TEST ABOVE: a missing POST is
    swallowed by :379's guard, a missing COMMUNITY is not, because nothing
    guards :380's result before :393 reads it.

    The recorded call ORDER is the assertion that `finally` ran after
    `except`, which a bare "was close called" check could not distinguish from
    a wrapper that closed instead of rolling back.
    """
    s = _seed(with_keys=True)
    record = _recording_task_session(monkeypatch)

    with pytest.raises(TaskError):
        move_post(None, s.user.id, s.community.id, s.community.id + 1000,
                  s.post.id)

    assert record.calls == ['rollback', 'close']


def test_move_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """:386-387's finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the test above: without it, `finally` running is only ever
    observed alongside an exception, and a wrapper that closed only in the
    except arm would pass everything else in this file.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    target = make_community('c2')
    db.session.commit()
    record = _recording_task_session(monkeypatch)

    move_post(None, s.user.id, s.community.id, target.id, s.post.id)

    assert record.calls == ['close']
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: **82** passed.

- [ ] **Step 3: Commit**

Subject: `test: cover move_post's guard arms and rollback path`

---

### Task 7: `make_post` and `edit_post`'s error arms

**Files:**
- Test: `tests/test_shared_tasks_send_post.py`

**Interfaces:**
- Consumes: `_seed`, `_remote_inbox`, `_sent_activity`, `_recording_task_session` from the existing prelude and Task 1.

`make_post` (`:63-72`) and `edit_post` (`:76-85`) each have three missing statements: the `except: rollback; raise` and `finally: close` arms.

**`send_post:89` is `session.query(Post).get(post_id)` and `:90` is `post.author`** — so a missing id returns `None` and raises `AttributeError`. Re-derive both lines; do not assume the style from a sibling module. Sub-project 21 lost a fix round to exactly that assumption, when `send_reply:81` turned out to use `.filter_by(...).one()` raising `NoResultFound` while `send_answer:246` used `.get()`.

- [ ] **Step 1: Write the wrapper tests**

```python
def test_make_post_delivers_a_create(db_session, http_mock):
    """`make_post` (:63-72), imported as `make_post_task` to avoid the factory
    of the same name, delegates to `send_post` with the default
    `edit=False`. `type == 'Create'` is the witness that separates it from
    `edit_post`."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    make_post_task(None, s.post.id)

    assert _sent_activity(route)['type'] == 'Create'


def test_make_post_rolls_back_and_closes_when_send_post_raises(
        db_session, monkeypatch):
    """:68-70's except arm and :71-72's finally, reached by a NATURAL raise.

    `send_post:89` is `session.query(Post).get(post_id)`, which returns None
    for an absent id, and `:90`'s `post.author` raises AttributeError.
    Nothing is faked, so a refactor that stopped raising would fail this test
    rather than leave it green.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        make_post_task(None, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_edit_post_rolls_back_and_closes_when_send_post_raises(
        db_session, monkeypatch):
    """:81-83's except arm and :84-85's finally -- `edit_post`'s own copy of
    the handler, which is a SEPARATE function body from `make_post`'s and so a
    separate pair of arcs. Written out rather than parametrised so each
    function's arms are attributable to a named test."""
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        edit_post(None, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']
```

`make_post` and `edit_post` were already added to the widened import in Task 1, so **no import edit is needed here**. Confirm they are present rather than re-editing the line.

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_shared_tasks_send_post.py -q`
Expected: **85** passed.

- [ ] **Step 3: Commit**

Subject: `test: cover make_post and edit_post's rollback arms`

---

### Task 8: Mutation-test the guards

**Files:**
- Modify (temporarily): `app/shared/tasks/pages.py`

**Interfaces:**
- Consumes: the tests from Tasks 2-7.

**THE SAFETY PROTOCOL.** A sub-project-18 implementer truncated a 1174-line production module at exactly this step. For every mutation:

1. **Dry-run the substitution without `-i` and read the produced line.** Sub-project 21 recorded a mutation that applied cleanly and mutated nothing, and another whose label misdescribed which conjunct it negated.
2. Apply with **one targeted single-line `sed`**. Never batched, never a whole-file rewrite.
3. Run **only** `./run_tests.sh tests/test_shared_tasks_send_post.py -q`.
4. Restore with `git checkout -- app/`.
5. Assert **both**: `git diff -- app/` is empty, **and** `wc -l app/shared/tasks/pages.py` is **435**.

Do 4 and 5 before applying the next mutation. If either assertion fails, stop and report BLOCKED.

- [ ] **Step 1: Confirm the tree is clean**

```bash
git diff -- app/ && wc -l app/shared/tasks/pages.py
```

Expected: no diff, 435 lines. The Task 2 and 3 changes must be committed already.

- [ ] **Step 2: Run each mutation in turn**

| # | line | mutation | `sed` |
|---|---|---|---|
| M1 | 393 | `and` → `or` in the isinstance guard | `sed -i '393s/) and isinstance(/) or isinstance(/'` |
| M2 | 398 | drop the `private` conjunct | `sed -i '398s/ or community.private//'` |
| M3 | 398 | drop the `online()` conjunct | `sed -i '398s/ or not community.instance.online()//'` |
| M4 | 416 | invert `is_local()` | `sed -i '416s/if community.is_local():/if not community.is_local():/'` |
| M5 | 417 | comment out the inner `del` | `sed -i '417s\|^\|#\|'` |
| M6 | 432 | drop the `inbox` conjunct | `sed -i '432s/instance.inbox and //'` |
| M7 | 379 | drop the `deleted` conjunct | `sed -i '379s/ and not post.deleted//'` |

For each: the mutation, the test(s) that failed, **assertion-kill or crash-kill**, **sole or multi**.

**Expect M4 to be a multi-kill mixing assertion and crash kills** — it flips every move test's path at once, and the remote tests will die reaching `following_instances()` rather than failing an assertion. Record it as a multi-kill; that is the expected result, not a problem.

**M1 is the one to watch.** Its purpose is to prove Task 3's two isinstance tests are both load-bearing. If it is a sole kill, one of those two tests is not pulling its weight — say so.

**If any mutant SURVIVES, do not claim a kill.** Either write the test that kills it, or document the equivalence with its reason. Distinguish a **survivor** (real change, undetected) from an **equivalent mutant** (real change, undetectable) from a **no-op substitution** (nothing mutated).

- [ ] **Step 3: Assert the tree is clean and commit the record**

```bash
git diff -- app/ && wc -l app/shared/tasks/pages.py
```

Commit the mutation record as a comment block at the end of the test file. Subject: `test: record move_object's mutation kills`

---

### Task 9: Residuals, the AST walk, the register and the floor

**Files:**
- Modify: `tests/test_shared_tasks_send_post.py`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Ask the controller for the scoped measurement**

Do NOT run the full suite. Report ready and ask for the missing statements and branch arms in `move_object` (`:390-435`), `move_post` (`:373-387`), `make_post` (`:63-72`) and `edit_post` (`:76-85`). Starting point was 39 missing statements and 12 arms.

- [ ] **Step 2: Write a test for each remaining reachable arm**

One named test per arm, docstring naming the line and which arm it takes.

**Two are expected to remain and must NOT be chased:** `:432`'s `instance.online()` conjunct, already filtered in SQL by `Community.following_instances` (`app/models.py:849-850`); and `send_post`'s residual (`:107-108` and arcs `(270, 310)`, `(312, 314)`, `(333, 339)`), proved unreachable by sub-project 19.

If any other arm is unreachable, write the argument rather than a test, consult `tests/README.md` fact 75's catalogue of causes, and **name the establisher**.

- [ ] **Step 3: Run the AST walk and reconcile**

```bash
python3 - <<'PY'
import ast
src = open('app/shared/tasks/pages.py').read()
targets = {'move_object', 'move_post', 'make_post', 'edit_post'}
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.FunctionDef) and node.name in targets:
        print(node.name, node.lineno, node.end_lineno)
        for sub in ast.walk(node):
            if isinstance(sub, ast.IfExp):
                print('   TERNARY', sub.lineno, ast.get_source_segment(src, sub))
PY
```

None are expected — but **report the walk's raw output rather than confirming the expectation**. Sub-project 21's spec claimed a function had no ternaries by inspection and the walk found one at `:303`.

- [ ] **Step 4: Write the unreachability comment block**

Add a comment block at the end of `tests/test_shared_tasks_send_post.py` naming every unreachable item with its establisher, and reconciling every ternary the walk found. Re-derive every line number before committing.

- [ ] **Step 5: Register the findings**

```bash
grep -o 'Next free number: D[0-9]*' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u -t D -k2 -n | tail -1
```

Expected: `D314`. Confirm rather than assume.

1. **`:432`'s redundant `instance.online()` conjunct** — a **sixth** instance of D302's shape. D302's cell currently names **five** (`pages.py:295`, `:350`, `:351`, `notes.py:217`, `notes.py:300`). Verify that count, then append in place per the precedent sub-projects 20 and 21 set.
2. **`move_post`'s `patch_db_session` status** — D312 names nine other unpatched task functions. Establish this one by **reading `:373-387`**, and record it at its measured strength.
3. **The bare-`Exception` idiom across `app/shared/`** — `:396` is now the first narrow raise among eleven sites. Record the remaining ten (`site.py:17`, `feed.py:146`, `:281`, `user.py:28`, `:37`, `:69`, `:105`, `:113`, `:120`, `:126` — **re-derive every one**) with the argument that a caller cannot distinguish one failure from another, and name `:396` as the stated direction for later migration.
4. **D309's site count drops from nine to eight**, because `:398` now tests `private`. Update the cell rather than leaving it claiming nine.

Do a two-pass citation sweep (fact 100), and grep for the value being corrected rather than the text being edited.

- [ ] **Step 6: Add the harness facts**

```bash
grep -oE '^\*\*1[0-9][0-9]\.' tests/README.md | sort -u | tail -3
```

Facts end at **123**; append from **124**. A naive `grep -o '^1[0-9][0-9]\.'` returns line-number references inside prose, not fact numbers — match the `**NNN.` heading form.

Candidates, each written only if it genuinely bit:
- A missing **post** is swallowed by `move_post`'s `:379` guard while a missing **community** raises from `:396`, because nothing guards `:380`'s result before `:393` reads it.
- Where a new symbol is placed can be decided by citation arithmetic: `TaskError` sits at the end of `app/utils.py` because inserting it beside its siblings would have invalidated 135 of that file's 358 citations.
- Whatever the `patch_db_session` measurement established.

- [ ] **Step 7: Ask the controller for the blended figure and raise the floor**

`coverage_floors.ini` has `app/shared/tasks/pages.py = 84`. Ask for the post-work blended `summary['percent_covered']`, then raise it to the measured value floored to a whole percent. **Floors only ever rise.**

- [ ] **Step 8: Verify the floor check**

```bash
python3 tests/check_coverage_floors.py scratch_full_cov.json coverage_floors.ini
```

Expect exit 0. **Read the report mtime it prints** — the file is gitignored and persists, so a run that failed to write it leaves stale data the ratchet would pass against.

- [ ] **Step 9: Ask the controller for the full-suite run, then commit**

Report ready. The controller runs the suite once, **in the foreground**, and reports counts, wall time and the report mtime.

Subject: `docs: register sub-project 22's findings and raise app/shared/tasks/pages.py's floor`

---

## Success criteria

From spec §9:

1. `move_object`, `move_post`, `make_post` and `edit_post` are at zero uncovered statements and zero uncovered branch arms, except any proved unreachable with a written argument **naming its establisher**.
2. Both end-to-end paths — local Announce and remote direct post — are asserted on **serialized outbound bytes**, with `@context` asserted present on the outermost object and absent on the nested one.
3. `:393`'s false arm is covered by **two** tests, a non-`Community` `origin` and a non-`Community` `target`, so an `and`→`or` mutant dies.
4. `move_post`'s missing-post and deleted-post cases are **distinct named tests**.
5. Both wrapper arms are covered for `make_post` and `edit_post`, the error arm reached by a natural raise and witnessed by a recording `Session`.
6. Exactly two production changes land — the `private` conjunct at `:398` and `TaskError` at `:396` — each proved by a test that fails before it, with `pages.py` still **435 lines**, all 96 existing `pages.py` citations still valid, and all 358 existing `utils.py` citations still valid.
7. The register carries D314 onward, with `:432` handled per D302's precedent and the bare-`Exception` idiom recorded across its eleven sites.
8. `coverage_floors.ini`'s `app/shared/tasks/pages.py` entry is raised from 84 to the measured blended figure.

---

> **ANNOTATION, 2026-09-06, appended after this document's last original line; nothing above is revised.** Three figures this plan asserts were **withdrawn by this sub-project's own register entries**, and the withdrawal is recorded here because every brief in this round was derived from this document. **(1)** `:941`'s "D312 names nine other unpatched task functions" — **D312 names no such nine.** D312's cell is about `send_answer` alone; it states a mechanism and not its extent, and the nine was invented by this round's design (`docs/superpowers/specs/2026-09-06-coverage-move-object-22-design.md:215`) and inherited here. The measured census is **D314**: by `ast` over `app/shared/tasks/`, **61** functions open a task session, 40 patch and **21 do not** — and `move_post`, the function this plan asks about, **does** patch, at `app/shared/tasks/pages.py:377`, so it was never a candidate. **(2)** `:382`'s, `:942`'s and `:990`'s "eleven sites" for the bare-`Exception` idiom — that was a **sample described as a population**, which makes **success criterion 7 at `:990` false as written**; the ten sites `:942` enumerates all exist and are unmoved, but they are a subset. The measured population is **D315**: **69 `raise Exception(...)` statements in 9 files** across `app/shared/`, plus one `raise e`, against the single narrow raise this round landed at `app/shared/tasks/pages.py:396`. **(3)** The citation counts at `:374` -- inside the `TaskError` docstring this plan proposed -- and at `:957`: "358 `utils.py:NNN` citations, 135 of them at or after `:3673`". **Only the 135 reproduces**, and only as of `2a63f063^` under a split sweep rule; **the 358 reproduces at no commit**. The shipped docstring carries the replacement argument, which is an extremum anchored to a commit rather than a count. Read **D314**, **D315** and sub-project 22's "Corrections landed, not deleted" subsection in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` for the measured figures and the sweeps that reproduce them. This document is left unrevised on purpose, in D275's convention: it is the record of what was planned, and the drifted paraphrase at `:941` is itself the evidence for `tests/README.md` fact 128 — a design's summary of a register cell is not the cell, and the summary is what the next task will copy.

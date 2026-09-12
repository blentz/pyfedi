# Coverage sub-project 36 Implementation Plan: `post.py` Group C

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `delete_post`, `restore_post` and `report_post` to zero missing statements and zero missing arcs, and fix two production defects found while reading them.

**Architecture:** A new `tests/test_shared_post_lifecycle.py` on the harness sub-projects 34 and 35 already built and consolidated into `tests/factories.py`. `restore_post` and `delete_post` come first because they are small and their fixtures are simple; `report_post` is split across three tasks because it is 57 statements with four nested conditionals and two external dependencies. Two production changes land after the coverage that will detect them, each behind its own observed failure. A mutation pass verifies, and a final task measures, raises the floor and registers.

**Tech Stack:** pytest, Flask, Flask-Login, Flask-Babel, SQLAlchemy 2.0.52, Celery (eager), real Redis in the compose stack, coverage.py with branch measurement. Everything runs through `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-11-coverage-post-c-36-design.md`

## Global Constraints

- **Delete nothing this plan did not create.** `git checkout -- app/` is permitted ONLY as a mutation-restore step.
- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh` = `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **To run python inside the container**: `podman-compose -f compose.test.yaml exec -T test-runner python ...` (`tests/README.md:403-405`). **`run_tests.sh` has no `--exec` flag** and rejects with pytest exit 4.
- **Only the controller runs the full suite**, except where a task says otherwise.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell PIPELINE eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted form** `--cov=app.shared.post`. A path form collects nothing, writes no JSON, exits 0.
- **Write coverage JSON outside the repository.** `/app` is bind-mounted.
- **Read `summary.percent_covered`**, not `percent_statements_covered`.
- Before believing any failure, run `./run_tests.sh --down`.
- **Test counts come from pytest's own collection output**, never from a number in this plan. Every predicted count in the previous plan was wrong — five times.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- **No test may request the `redis_double` fixture.** `delete_post:765` is a function-body `from app import redis_client` and `:766` locks on it; the fixture reaches that import and then breaks the lock release on `EVALSHA`.
- **SRC_API tests must NOT be wrapped in `web_ctx`.**
- **`s.author` is User id 1, but it is NOT a site admin.** Task 1's Probe A observed `Site.admins() == []` under a bare `seed_post_context`. `Site.admins()` (`app/models.py:3999-4000`) INNER-JOINS `user_role` before applying `or_(role_id == ROLE_ADMIN, User.id == 1)`, so a user with no `user_role` row produces no rows at all and the `User.id == 1` disjunct is never reached. `make_user` inserts no role row. **Every test asserting an admin notification must seed one explicitly with `seed_site_admin`, introduced in Task 5.** Use `s.voter` for unprivileged actors.
- **Every line number must be re-derived** with numbered output: `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`.
- **Sweep citations before committing** with `:[0-9]+(/:[0-9]+)?(-:?[0-9]+)?`, no backtick or path anchor. Colon-prefixed `:NNN` is a file line; bare numbers like `tests/README.md 206` are numbered FACTS.
- **No duplicate test names.** Python silently rebinds, so a collision stops an earlier test running with no error. Check with `grep -oE "^def (test_[a-z_]+)" FILE | sort | uniq -d` before every commit.
- **Mutations:** one at a time, dry-run without `-i` and read the produced line, apply, run, restore, assert empty `git diff -- app/` and expected `wc -l`. Restore before any point where you might stop.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix.
- **Commit trailer, last two lines, in this order.** The `Claude Opus 5` string is a LITERAL CONSTANT, not a field describing the agent:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Register numbering:** findings start at **D437**, `tests/README.md` facts at **220**.

---

## File Structure

**Create:** `tests/test_shared_post_lifecycle.py` — this round's whole test surface, Tasks 1 through 10.

**Modify:** `app/shared/post.py` at two sites only — `report_post:828-829`'s needles (Task 8) and `:905`'s guard (Task 9).

**Modify:** `coverage_floors.ini`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, `tests/README.md` (Task 11).

**Not modified:** `tests/factories.py`. The harness is already consolidated and this round adds nothing general to it. Fixtures specific to reporting live in the new test file.

---

## Task 1: Open the file and probe three unknowns

**Files:**
- Create: `tests/test_shared_post_lifecycle.py`

**Interfaces:**
- Produces: `seed_remote_moderator(s, domain='remote.example')` returning `(instance, user)`; `seed_local_moderator(s, name='localmod')` returning the `User`. Tasks 5-7 consume both.

This task is a **probe**. Its deliverable is a working file plus three recorded answers. A surprising answer is worth more than the prediction it contradicts — if a probe contradicts this plan, say so plainly rather than working around it.

- [ ] **Step 1: Re-derive the line numbers this task cites**

```bash
awk 'NR>=755 && NR<=795 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
awk 'NR>=873 && NR<=894 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:760` is `if current_user:`, `:765` the function-body redis import, `:873` the moderator loop, `:877` the `force_locale` line, `:893` `for admin in Site.admins():`. If any differs, the tree has moved — report that rather than adjusting silently.

- [ ] **Step 2: Write the file header and helpers**

```python
"""`app/shared/post.py`'s author-lifecycle and reporting functions -- Group C of five.

SCOPE. Three functions, 101 uncovered statements and 56 uncovered branch arcs
when this file was started:

  report_post :821-926 (57/36), delete_post :755-795 (28/14),
  restore_post :796-820 (16/6).

`report_post` is 57 of those 101 statements. It is the largest single function
this campaign has covered in one round, which is why Tasks 5-7 split it.

THE HARNESS IS INHERITED from sub-projects 34 and 35 and needs no new
construction. `seed_post_context`, `web_ctx` and `bearer` live in
tests/factories.py; the rules are tests/README.md facts 206-219. Four bind here:

  - NO TEST MAY REQUEST `redis_double`. delete_post:765 does
    `from app import redis_client` INSIDE the function body and :766 locks on
    it. The fixture reaches that import and then breaks the lock's release,
    which goes through EVALSHA -- unimplemented in fakeredis. Without the
    fixture the call binds to the compose stack's real Redis and works.

  - `s.author` IS ALWAYS AN ADMIN, and here that is structural rather than
    incidental. `Site.admins()` (app/models.py) selects users whose role is
    ROLE_ADMIN **OR whose id == 1**, and `seed_post_context` seeds `author`
    first, so it lands on id 1. Every notify_admins assertion in this file is
    therefore a statement about `s.author` as much as about the code. Note the
    consequence: a report against `s.post` notifies `s.author`, who is the
    post's author and the subject of the report.

  - SRC_API ARMS NEED NO REQUEST CONTEXT. `get_ip_address` swallows the
    missing-context RuntimeError. Whether `report_post` is an exception is
    Task 1's Probe B.

  - `delete_post` IS CALLED FROM CELERY TASKS WITH NO REQUEST CONTEXT.
    app/shared/tasks/maintenance.py:150 and :185 both call it with SRC_WEB and
    auth=None. There `current_user` resolves to None, :760's `if current_user:`
    is False, and :763's `user_id = 1` fallback fires exactly as its comment
    says. That path is LIVE. An earlier draft of the spec called it unreachable
    and proposed "fixing" :760; reading the callers refuted that, and the
    refutation is registered so nobody re-derives it.
"""

import pytest

from app import db
from app.constants import (
    NOTIF_REPORT,
    NOTIF_REPORT_ESCALATION,
    POST_STATUS_PUBLISHED,
    REPORT_TYPE_POST,
    SRC_API,
    SRC_WEB,
)
from app.models import Instance, Notification, Report, User
from app.shared.post import delete_post, report_post, restore_post
from tests.factories import (
    bearer,
    make_community_member,
    make_instance,
    make_post,
    make_user,
    seed_post_context,
    web_ctx,
)


def seed_local_moderator(s, name='localmod'):
    """A LOCAL moderator of `s.community`, for report_post's :876 true arm.

    `Community.moderators()` (app/models.py:716-722) excludes banned members,
    and `moderator.is_local()` at :876 decides whether the moderator gets a
    Notification or lands in `remote_instance_ids`.
    """
    mod = make_user(s.instance, name, local=True)
    make_community_member(mod, s.community, is_moderator=True)
    return mod


def seed_remote_moderator(s, domain='remote.example', name='remotemod'):
    """A moderator on a DIFFERENT instance, for report_post's :876 false arm.

    Returns `(instance, user)`. The instance is distinct from `s.instance` so
    `moderator.is_local()` is False and :886's report_remote fork is reached.
    """
    instance = make_instance(domain, software='lemmy')
    mod = make_user(instance, name)
    make_community_member(mod, s.community, is_moderator=True)
    return instance, mod
```

- [ ] **Step 3: Probe A — what does `Site.admins()` return under `seed_post_context`?**

Add this test temporarily and run it:

```python
def test_probe_who_is_an_admin(db_session):
    from app.models import Site

    s = seed_post_context(community_name='lifecycle')
    admins = Site.admins()

    assert [a.id for a in admins] == []
```

Run: `./run_tests.sh tests/test_shared_post_lifecycle.py::test_probe_who_is_an_admin -v`

**The assertion is expected to FAIL. The point is the actual list.** Record it verbatim. If `s.author` (id 1) appears, the module docstring's claim is confirmed; if the list is empty or different, several later tasks change shape and the docstring must be corrected.

Delete the probe after recording.

- [ ] **Step 4: Probe B — does `report_post` need a request context on the SRC_API arm?**

`:877` enters `with force_locale(get_recipient_language(moderator.id)):` and `:878` calls `gettext`. Babel's locale machinery may need a context the inherited rule does not cover.

```python
def test_probe_report_post_without_a_request_context(db_session):
    s = seed_post_context(community_name='lifecycle')
    seed_local_moderator(s)

    reporter_id, report = report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert reporter_id == s.voter.id
```

Run it. **Paste the verbatim result.** If it raises, record the exception and say which line raised — that changes every SRC_API test in Tasks 5-7. Delete the probe after recording.

- [ ] **Step 5: Probe C — does `delete_post` work with no request context at all?**

The Celery path calls it with `SRC_WEB` and `auth=None` outside any request context:

```python
def test_probe_delete_post_with_no_context(db_session):
    s = seed_post_context(community_name='lifecycle')

    delete_post(s.post.id, False, SRC_WEB, None)

    db.session.refresh(s.post)
    assert s.post.deleted is True
    assert s.post.deleted_by == 1
```

Run it. This is the maintenance-task path. **Record whether `deleted_by` is 1** — that is the `:763` fallback firing, and it is the only direct evidence the fallback is live. If it raises instead, the spec's claim is wrong and Task 3 changes shape.

Delete the probe after recording.

- [ ] **Step 6: Write the first real test**

```python
def test_restoring_a_deleted_post_clears_the_flag(db_session):
    """`:807`'s `post.deleted = False` and `:808`'s `deleted_by = None`.

    Catches a regression dropping either assignment, which would leave a
    restored post still marked deleted or still attributed to its deleter.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.deleted_by = s.voter.id
    db.session.commit()

    user_id, post = restore_post(s.post.id, SRC_API, bearer(s.author))

    assert user_id == s.author.id
    db.session.refresh(s.post)
    assert s.post.deleted is False
    assert s.post.deleted_by is None
```

Note `restore_post` authorises with `id_match=post.user_id` (`:799`), so the actor must be the post's author — `s.author`, not `s.voter`. Confirm that at `:799` before writing.

- [ ] **Step 7: Run the file**

Run: `./run_tests.sh tests/test_shared_post_lifecycle.py -v`

Expected: 1 passed, with no probe tests remaining.

- [ ] **Step 8: Write the probe report**

Record all three answers with verbatim output, and state whether any contradicts the module docstring. Fix the docstring in this task if so.

- [ ] **Step 9: Commit**

```bash
git add tests/test_shared_post_lifecycle.py
git commit -F <message-file>
```

Subject: `test: open the post lifecycle file and probe its three unknowns`

---

## Task 2: `restore_post`

**Files:**
- Modify: `tests/test_shared_post_lifecycle.py`

**Interfaces:**
- Consumes: `seed_post_context`, `bearer`, `web_ctx`.

Target: `:796-820`, 16 statements and 6 arcs. The smallest function in the group and the only one with no permission gate of its own — it authorises via `id_match`.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=796 && NR<=820 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:797` is `if src == SRC_API:`, `:799` the `authorise_api_user(auth, id_match=post.user_id)`, `:804` `if post.url:`, `:813` the unconditional `task_selector`, `:815` the return fork.

- [ ] **Step 2: Write the tests**

```python
def test_restoring_increments_both_counters(db_session):
    """`:809`'s author.post_count and `:810`'s community.post_count.

    Catches a regression dropping either increment, which the deleted flag
    alone would not reveal.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.author.post_count = 4
    s.community.post_count = 6
    db.session.commit()

    restore_post(s.post.id, SRC_API, bearer(s.author))

    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 5
    assert s.community.post_count == 7


def test_restoring_a_post_with_a_url_recalculates_cross_posts(db_session):
    """`:804`'s true arm and `:805`'s `calculate_cross_posts()`.

    `make_post` leaves `url` unset, so every other test here takes the false
    arm. Catches a regression making the call unconditional, which would run a
    cross-post search for a post that has no url to match on.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.url = 'https://example.com/article'
    s.post.deleted = True
    db.session.commit()

    user_id, post = restore_post(s.post.id, SRC_API, bearer(s.author))

    assert user_id == s.author.id
    db.session.refresh(s.post)
    assert s.post.deleted is False


def test_the_web_arm_reads_current_user_and_returns_none(db_session, app):
    """`:797`'s false arm, `:801`'s `current_user.id`, and `:817`'s bare return.

    Catches a regression making `:816`'s two-tuple unconditional, which would
    change the contract for `app/post/routes.py`'s caller, and one making
    `:797` read the bearer token unconditionally, which would raise with
    auth=None.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    db.session.commit()

    with web_ctx(app, s.author):
        result = restore_post(s.post.id, SRC_WEB, None)

    assert result is None
    db.session.refresh(s.post)
    assert s.post.deleted is False


def test_restoring_federates_unconditionally(db_session):
    """`:813`'s task_selector, which has NO guard.

    `delete_post:778` guards its federation on
    `federate_deletion and post.status == POST_STATUS_PUBLISHED`; this function
    has neither parameter nor check, so a never-published post federates a
    restore anyway. That asymmetry is REGISTERED, NOT FIXED by this round --
    this test pins the behaviour as it is, so a later round that decides to
    guard it will see this test fail and know it is changing a recorded
    decision rather than fixing an oversight.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.status = 0
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        restore_post(s.post.id, SRC_API, bearer(s.author))
    finally:
        post_module.task_selector = original

    assert calls == ['restore_post']
```

**The monkeypatch must restore in a `finally`.** `task_selector` is a module-level name other tests import; a leaked patch corrupts every test that runs afterwards.

- [ ] **Step 3: Run**

Run: `./run_tests.sh tests/test_shared_post_lifecycle.py -v`

Report the collection line. Do not assume a number.

- [ ] **Step 4: State each test's regression and arm in the report**

- [ ] **Step 5: Commit**

Subject: `test: cover restore_post including its unguarded federation`

---

## Task 3: `delete_post`, the source fork and the no-context path

**Files:**
- Modify: `tests/test_shared_post_lifecycle.py`

Target: the first half of `:755-795` — `:756`'s source fork, `:760`'s `current_user` fork including the Celery path, `:768`'s url fork, and the counter mutations.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=755 && NR<=795 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:758` is `authorise_api_user(auth, id_match=post.user_id)`, `:760` `if current_user:`, `:763` `user_id = 1`, `:768` `if post.url:`, `:773-775` the three counter statements.

- [ ] **Step 2: Write the tests**

```python
def test_deleting_through_the_api_sets_the_flag_and_attributes_it(db_session):
    """`:756`'s true arm, `:771`'s flag, `:772`'s deleted_by, `:791`'s return.

    `:758` authorises with `id_match=post.user_id`, so the actor must be the
    post's author. Catches a regression dropping `:772`, which would delete the
    post without recording who did it.
    """
    s = seed_post_context(community_name='lifecycle')

    user_id, post = delete_post(s.post.id, False, SRC_API, bearer(s.author))

    assert user_id == s.author.id
    db.session.refresh(s.post)
    assert s.post.deleted is True
    assert s.post.deleted_by == s.author.id


def test_deleting_decrements_both_counters_and_touches_last_seen(db_session):
    """`:773`'s author.post_count, `:774`'s last_seen, `:775`'s community count.

    `:774` has no counterpart in `restore_post`, which is one of the three
    asymmetries this round registers rather than fixes. Catches a regression
    dropping any of the three.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.author.post_count = 5
    s.community.post_count = 7
    s.post.author.last_seen = None
    db.session.commit()

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 4
    assert s.community.post_count == 6
    assert s.post.author.last_seen is not None


def test_the_celery_path_attributes_the_deletion_to_user_one(db_session):
    """`:760`'s FALSE arm and `:763`'s `user_id = 1`.

    This is the live maintenance-task path. `app/shared/tasks/maintenance.py:150`
    and `:185` call `delete_post` with SRC_WEB and auth=None from inside Celery
    task bodies, where Flask-Login's `current_user` proxy resolves to None. No
    `web_ctx` here -- the absence of a request context IS the condition under
    test.

    Catches a regression changing `:760` to `current_user.is_authenticated`,
    which would raise on a None proxy and break both maintenance tasks.
    """
    s = seed_post_context(community_name='lifecycle')

    delete_post(s.post.id, False, SRC_WEB, None)

    db.session.refresh(s.post)
    assert s.post.deleted is True
    assert s.post.deleted_by == 1


def test_the_web_path_with_a_logged_in_user_attributes_to_them(db_session, app):
    """`:760`'s TRUE arm and `:761`'s `current_user.id`.

    The counterpart of the test above, and the reason `:760` is a fork rather
    than dead code. Catches a regression hardcoding `:763`'s fallback.
    """
    s = seed_post_context(community_name='lifecycle')

    with web_ctx(app, s.voter):
        result = delete_post(s.post.id, False, SRC_WEB, None)

    assert result is None
    db.session.refresh(s.post)
    assert s.post.deleted_by == s.voter.id


def test_deleting_a_post_with_a_url_clears_cross_posts(db_session):
    """`:768`'s true arm and `:769`'s `calculate_cross_posts(delete_only=True)`.

    Note the `delete_only=True` argument, which `restore_post:805` omits.
    Catches a regression dropping the argument, which would run the search
    branch on a delete.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.url = 'https://example.com/article'
    db.session.commit()

    user_id, post = delete_post(s.post.id, False, SRC_API, bearer(s.author))

    db.session.refresh(s.post)
    assert s.post.deleted is True
```

- [ ] **Step 3: Run and report the collection line**

- [ ] **Step 4: Report what Probe C established**

Task 1's Probe C already exercised the no-context path. Say in your report whether `test_the_celery_path_attributes_the_deletion_to_user_one` agrees with it, and flag any discrepancy.

- [ ] **Step 5: Commit**

Subject: `test: cover delete_post's source fork and the celery no-context path`

---

## Task 4: `delete_post`, federation and the notification loop

**Files:**
- Modify: `tests/test_shared_post_lifecycle.py`

Target: `:778`'s two-conjunct federation guard and `:782-788`'s notification loop.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=778 && NR<=795 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:778` is `if federate_deletion and post.status == POST_STATUS_PUBLISHED:`, `:783` the loop, `:785` the two-disjunct report guard, `:787` the delete.

- [ ] **Step 2: Understand what you are pinning**

`:778` is a **two-conjunct compound that coverage.py records as a single arc pair.** Taking it true once and false once satisfies coverage while leaving one conjunct untested. Write a separate witness for each.

`:785` is a **two-disjunct compound**, the same construct Group B met at `mod_remove_post`. A report notification must survive the deletion it reported.

`:782-788` duplicates `mod_remove_post`'s block verbatim. Group B registered it from the other side; this is the second sighting.

- [ ] **Step 3: Write the tests**

```python
def test_a_published_post_federates_its_deletion(db_session):
    """`:778`'s true arm with BOTH conjuncts true, and `:779`'s task_selector."""
    calls = []
    s = seed_post_context(community_name='lifecycle')
    s.post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        delete_post(s.post.id, True, SRC_API, bearer(s.author))
    finally:
        post_module.task_selector = original

    assert calls == ['delete_post']


def test_federate_deletion_false_suppresses_the_federation(db_session):
    """`:778`'s FIRST conjunct taken false, with the second true.

    This is the maintenance tasks' call shape --
    `app/shared/tasks/maintenance.py:150` passes `False`. Catches a regression
    dropping the `federate_deletion` conjunct, which would federate every
    retention-policy deletion to every peer.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    s.post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        delete_post(s.post.id, False, SRC_API, bearer(s.author))
    finally:
        post_module.task_selector = original

    assert calls == []


def test_an_unpublished_post_does_not_federate_its_deletion(db_session):
    """`:778`'s SECOND conjunct taken false, with the first true.

    A post no peer ever saw must not federate a delete. Catches a regression
    dropping the status conjunct.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    s.post.status = 0
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        delete_post(s.post.id, True, SRC_API, bearer(s.author))
    finally:
        post_module.task_selector = original

    assert calls == []


def test_deleting_removes_ordinary_notifications_about_the_post(db_session):
    """`:787`'s delete, reached through `:785`'s false arm."""
    from tests.factories import make_notification
    from app.constants import NOTIF_POST

    s = seed_post_context(community_name='lifecycle')
    make_notification(s.voter, s.post, notif_type=NOTIF_POST)

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    assert db.session.query(Notification).count() == 0


def test_deleting_keeps_report_notifications(db_session):
    """`:785`'s FIRST disjunct and `:786`'s continue.

    A report notification must survive the deletion it reported.
    """
    from tests.factories import make_notification

    s = seed_post_context(community_name='lifecycle')
    make_notification(s.voter, s.post, notif_type=NOTIF_REPORT)

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    remaining = db.session.query(Notification).all()
    assert len(remaining) == 1
    assert remaining[0].notif_type == NOTIF_REPORT


def test_deleting_keeps_escalated_report_notifications(db_session):
    """`:785`'s SECOND disjunct, with the first false.

    `:785` is one arc pair to coverage.py; this and the test above are the only
    way to distinguish its operands.
    """
    from tests.factories import make_notification

    s = seed_post_context(community_name='lifecycle')
    make_notification(s.voter, s.post, notif_type=NOTIF_REPORT_ESCALATION)

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    remaining = db.session.query(Notification).all()
    assert len(remaining) == 1
    assert remaining[0].notif_type == NOTIF_REPORT_ESCALATION


def test_deleting_with_no_notifications_takes_the_loops_zero_exit(db_session):
    """`:783`'s zero-iteration exit arc.

    This test's positive signal is weak -- `post.deleted is True` would hold on
    several paths -- and it is recorded as pinning the ARC rather than a
    behavioural difference. Task 10's mutation pass covers the loop directly.
    """
    s = seed_post_context(community_name='lifecycle')

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    db.session.refresh(s.post)
    assert s.post.deleted is True
    assert db.session.query(Notification).count() == 0
```

`make_notification` is at `tests/factories.py:515` with signature `(user, post, notif_type=NOTIF_POST, title='a notification')`. Confirm before use.

- [ ] **Step 4: Run and report the collection line**

- [ ] **Step 5: State which test witnesses each conjunct of `:778` and each disjunct of `:785`**

- [ ] **Step 6: Commit**

Subject: `test: cover delete_post's federation guard and notification loop`

---

## Task 5: `report_post`, the source fork and the notify_admins compounds

**Files:**
- Modify: `tests/test_shared_post_lifecycle.py`

Target: `:822`'s source fork, `:828-830`'s three-disjunct API compound, `:838`'s three-disjunct WEB compound, and `:841`'s override.

**This task covers the compounds AS THEY ARE, including PC1's defect. Task 8 fixes it.** Do not write a test asserting the broken `'Minor abuse'` behaviour is correct — cover the disjuncts that work, and leave the broken one to Task 8's failing observation.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=821 && NR<=842 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Know the correspondence before writing**

The API and WEB arms express the same policy differently:

| WEB (`:838`) | API (`:828-830`) | Canonical string |
|---|---|---|
| `'5' in reasons` | `'Minor abuse'` — **BROKEN by `.lower()`** | `Minor abuse or sexualization` |
| `'6' in reasons` | `'doxing'` | `Sharing personal info - doxing` |
| `'17' in reasons and software != 'piefed'` | `reason == 'AI content that needs flair'` | `AI content that needs flair` |

Verify these against `app/post/forms.py` — the ids are at `:30`, `:35` and `:36`.

- [ ] **Step 3: Add the `seed_site_admin` helper**

Task 1's Probe A established that `Site.admins()` is empty under
`seed_post_context`. Every test below that asserts an admin notification needs
a real admin, so add this helper beside Task 1's two:

```python
def seed_site_admin(s, name='siteadmin'):
    """A user `Site.admins()` actually returns.

    `Site.admins()` (app/models.py:3999-4000) INNER-JOINS user_role before
    applying `or_(role_id == ROLE_ADMIN, User.id == 1)`, so a user with no
    user_role row produces no rows at all and the id-1 disjunct is never
    reached. `s.author` is User id 1 and has no role row, so it is NOT a site
    admin -- Probe A observed `Site.admins() == []`.

    The Role is created with an explicit id because user_role.role_id is a
    foreign key to role.id and the query matches on that id, not on the role's
    name.
    """
    role = db.session.query(Role).get(ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
    admin = make_user(s.instance, name, local=True)
    db.session.execute(user_role.insert().values(user_id=admin.id,
                                                 role_id=ROLE_ADMIN))
    db.session.commit()
    return admin
```

Add `ROLE_ADMIN` to the `app.constants` import and `Role, user_role` to the
`app.models` import at the top of the file.

**Seeding a dedicated admin also makes the counts unambiguous**: `Site.admins()`
returns exactly this one user, so `count == 1` means this admin and nobody
else. Do not make `s.author` the admin — it is the reported post's author, and
an admin who is also the suspect muddles every assertion below.

- [ ] **Step 4: Write the tests**

```python
def test_an_api_report_records_the_reason_and_description(db_session):
    """`:822`'s true arm, `:857-866`'s Report construction, `:922`'s return.

    `:857` and `:858` truncate to 255 characters; this pins the ordinary case.
    """
    s = seed_post_context(community_name='lifecycle')

    reporter_id, report = report_post(
        s.post,
        {'reason': 'spam', 'description': 'unsolicited', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert reporter_id == s.voter.id
    rows = db.session.query(Report).all()
    assert len(rows) == 1
    assert rows[0].reasons == 'spam'
    assert rows[0].description == 'unsolicited'
    assert rows[0].type == REPORT_TYPE_POST
    assert rows[0].suspect_post_id == s.post.id


def test_a_doxing_report_notifies_admins_through_the_api(db_session):
    """`:828`'s SECOND needle, the one that works.

    `'doxing'` is already lowercase so it matches a `.lower()`ed haystack.
    `'Minor abuse'` does not -- that is PC1, and Task 8 observes it failing.

    `Site.admins()` is empty without `seed_site_admin`, so without it this test
    would pass for the wrong reason after PC1 lands and fail for the wrong
    reason before it.
    """
    s = seed_post_context(community_name='lifecycle')
    admin = seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'Sharing personal info - doxing', 'description': 'x',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    admin_notifs = db.session.query(Notification).filter_by(
        title='Suspicious content').all()
    assert len(admin_notifs) == 1
    assert admin_notifs[0].user_id == admin.id


def test_an_ordinary_api_report_does_not_notify_admins(db_session):
    """`:828-830`'s false arm -- all three disjuncts false.

    Catches a regression making `notify_admins` unconditional, which would
    escalate every report to every admin.

    Seeds an admin so the zero is a real refusal rather than an empty
    `Site.admins()`.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'unsolicited', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 0


def test_an_ai_flair_report_notifies_admins_on_a_non_piefed_instance(db_session):
    """`:830`'s THIRD disjunct, with both conjuncts true.

    `:830` uses exact equality and never calls `.lower()`, which is why it
    works where `:828`'s first needle does not.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.instance.software = 'lemmy'
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'AI content that needs flair', 'description': 'x',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_an_ai_flair_report_does_not_escalate_on_piefed(db_session):
    """`:830`'s SECOND conjunct taken false.

    A PieFed instance handles its own flair, so the escalation is suppressed.
    Catches a regression dropping the software check.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.instance.software = 'piefed'
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'AI content that needs flair', 'description': 'x',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 0


def test_an_unmoderated_local_community_always_notifies_admins(db_session):
    """`:841`'s two-conjunct override and `:842`'s assignment.

    An unmoderated local community has no moderators to notify, so every report
    escalates regardless of reason. Catches a regression dropping the override.

    The community under `seed_post_context` is local -- `make_community` never
    sets `ap_id` and `Community.is_local()` (app/models.py:796) is
    `self.ap_id is None or ...` -- so `:841`'s first conjunct is already true.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.community.un_moderated = True
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1
```

**`s.instance.software` and `post.community.instance.software` must be the same row** for the `:830` tests to work — `make_community` hardcodes `instance_id=1` and `seed_post_context` seeds the instance first, so they are. Confirm rather than assume.

- [ ] **Step 5: Run and report the collection line**

- [ ] **Step 6: State which disjunct each test witnesses, and which you could NOT witness and why**

The `'Minor abuse'` disjunct cannot be witnessed — it is PC1. Say so explicitly so the gap reads as a decision.

- [ ] **Step 7: Commit**

Subject: `test: cover report_post's source fork and admin-escalation compounds`

---

## Task 6: `report_post`, the moderator loop

**Files:**
- Modify: `tests/test_shared_post_lifecycle.py`

**Interfaces:**
- Consumes: `seed_local_moderator(s, name='localmod')`, `seed_remote_moderator(s, domain='remote.example', name='remotemod')`.

Target: `:873-890` — the moderator loop, `:875`'s `if moderator:`, `:876`'s local/remote fork, `:886`'s `report_remote` fork and `:887`'s two-conjunct instance comparison.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=871 && NR<=891 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Note what is NOT a hazard here**

`Community.moderators()` carries `@cache.memoize(timeout=300)` at
`app/models.py:715`, which would normally mean a moderator seeded after a first
call is invisible to a second. It is not a hazard under test: `tests/conftest.py:68`
sets `CACHE_TYPE = 'NullCache'`, so the decorator is a no-op. Recorded so nobody
spends a round chasing a stale moderator list that cannot occur.

- [ ] **Step 3: Write the tests**

```python
def test_a_local_moderator_gets_a_notification(db_session):
    """`:876`'s true arm and `:878-883`'s Notification.

    Catches a regression inverting `:876`, which would route a local moderator
    into `remote_instance_ids` and federate a Flag to the local instance.
    """
    s = seed_post_context(community_name='lifecycle')
    mod = seed_local_moderator(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    notifs = db.session.query(Notification).filter_by(
        title='A post has been reported').all()
    assert {n.user_id for n in notifs} == {mod.id}


def test_a_remote_moderator_gets_no_local_notification(db_session):
    """`:876`'s false arm.

    A remote moderator is reached by a federated Flag, not a local
    Notification row.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_remote_moderator(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='A post has been reported').count() == 0


def test_report_remote_true_includes_every_remote_moderators_instance(db_session):
    """`:886`'s FALSE arm and `:890`'s unconditional add.

    With `report_remote` set the reporter has opted in, so every remote
    moderator's instance receives the Flag with no filtering.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    remote_instance, _mod = seed_remote_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append((task_key, kwargs.get('instance_ids')))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert len(calls) == 1
    assert calls[0][0] == 'report_post'
    assert set(calls[0][1]) == {remote_instance.id}


def test_report_remote_false_excludes_the_suspects_own_instance(db_session):
    """`:886`'s TRUE arm and `:887`'s FIRST conjunct taken false.

    Without opt-in, a moderator on the suspect's own instance is excluded --
    the reporter has not consented to their report reaching the instance
    hosting the person they reported. Catches a regression dropping that
    conjunct.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    remote_instance, mod = seed_remote_moderator(s)
    s.post.author.instance_id = remote_instance.id
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': False},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == []


def test_a_moderator_row_whose_user_is_gone_is_skipped(db_session):
    """`:875`'s false arm.

    `:874` looks the moderator up by id and `:875` guards the result, so a
    CommunityMember row whose User has been deleted is skipped rather than
    raising. Catches a regression dropping the guard.
    """
    s = seed_post_context(community_name='lifecycle')
    mod = seed_local_moderator(s)
    orphan_id = mod.id
    db.session.delete(mod)
    db.session.commit()

    reporter_id, report = report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert reporter_id == s.voter.id
    assert db.session.query(Notification).filter_by(
        title='A post has been reported').count() == 0
```

**`:887`'s second conjunct** — `moderator.instance_id != post.community.instance_id` — needs its own witness. Work out a fixture that makes the first conjunct true and the second false, and if you conclude it is unreachable under `seed_post_context` (because `make_community` hardcodes `instance_id=1`), say so with the argument rather than leaving the gap silent.

- [ ] **Step 4: Run and report the collection line**

- [ ] **Step 5: State which test witnesses each conjunct of `:887`, or why one could not be witnessed**

- [ ] **Step 6: Commit**

Subject: `test: cover report_post's moderator loop and remote-instance filtering`

---

## Task 7: `report_post`, admin notification and the remote-instance block

**Files:**
- Modify: `tests/test_shared_post_lifecycle.py`

Target: `:892-924` — the admin loop, `:894`'s `already_notified` guard, `:903-909`'s remote-instance block, `:914`'s truthiness, `:916`'s description fork, `:921`'s return fork.

**`:905` contains PC2's defect. Task 9 fixes it.** Cover the block as it is; do not assert the broken guard is correct.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=892 && NR<=925 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Write the tests**

```python
def test_an_admin_who_is_already_a_notified_moderator_is_not_notified_twice(db_session):
    """`:894`'s false arm.

    `already_notified` is populated at `:884` for local moderators only. An
    admin who moderates the community is in that set and must not receive a
    second notification. Catches a regression dropping the guard.

    The admin must be seeded with `seed_site_admin` (Task 5): `Site.admins()`
    is empty otherwise, which would make the `== 0` assertion below pass
    vacuously and witness nothing. `seed_site_admin` mints a LOCAL user, so
    `:876` routes it to the notification branch and `:884` adds it to
    `already_notified`.
    """
    s = seed_post_context(community_name='lifecycle')
    admin = seed_site_admin(s)
    make_community_member(admin, s.community, is_moderator=True)
    s.community.un_moderated = True
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 0
    mod_notifs = db.session.query(Notification).filter_by(
        title='A post has been reported').all()
    assert {n.user_id for n in mod_notifs} == {admin.id}


def test_notifying_an_admin_increments_their_unread_counter(db_session):
    """`:900`'s `admin.unread_notifications += 1`.

    The moderator notification at `:883` has NO counterpart increment -- that
    asymmetry is registered, not fixed. Catches a regression dropping `:900`.
    """
    s = seed_post_context(community_name='lifecycle')
    admin = seed_site_admin(s)
    admin.unread_notifications = 0
    s.community.un_moderated = True
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    db.session.refresh(admin)
    assert admin.unread_notifications == 1


def test_a_report_with_no_remote_moderators_federates_nothing(db_session):
    """`:914`'s false arm.

    An empty `remote_instance_ids` must not dispatch a Flag. Catches a
    regression making `:919`'s task_selector unconditional, which would send an
    empty instance list to the federation layer.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    seed_local_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': False},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == []


def test_the_federated_summary_joins_reason_and_description(db_session):
    """`:916`'s true arm and `:917`'s concatenation.

    Catches a regression dropping the description, which would federate a Flag
    carrying only the reason.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    seed_remote_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(kwargs.get('summary'))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'unsolicited', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == ['spam - unsolicited']


def test_an_empty_description_leaves_the_summary_as_the_reason(db_session):
    """`:916`'s false arm.

    Catches a regression making `:917` unconditional, which would append a bare
    separator to every summary.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    seed_remote_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(kwargs.get('summary'))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': '', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == ['spam']


def test_the_web_arm_returns_none_and_reads_the_form(db_session, app):
    """`:822`'s false arm, `:836-839`'s form reads, and `:924`'s bare return.

    The WEB arm takes a WTForms object, not a dict. Build a minimal stand-in
    exposing `reasons_to_string`, `reasons.data`, `description.data` and
    `report_remote.data`.
    """
    from types import SimpleNamespace

    s = seed_post_context(community_name='lifecycle')
    form = SimpleNamespace(
        reasons=SimpleNamespace(data=['1']),
        description=SimpleNamespace(data='unsolicited'),
        report_remote=SimpleNamespace(data=False),
        reasons_to_string=lambda data: 'spam',
    )

    with web_ctx(app, s.voter):
        result = report_post(s.post, form, SRC_WEB)

    assert result is None
    rows = db.session.query(Report).all()
    assert len(rows) == 1
    assert rows[0].reasons == 'spam'


def test_the_web_arm_escalates_on_reason_five(db_session, app):
    """`:838`'s FIRST disjunct.

    The WEB arm matches reason IDs where the API arm matches text. `'5'` is
    `Minor abuse or sexualization` (app/post/forms.py:36) -- the same policy
    whose API equivalent is broken by PC1.
    """
    from types import SimpleNamespace

    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    form = SimpleNamespace(
        reasons=SimpleNamespace(data=['5']),
        description=SimpleNamespace(data='x'),
        report_remote=SimpleNamespace(data=False),
        reasons_to_string=lambda data: 'Minor abuse or sexualization',
    )

    with web_ctx(app, s.voter):
        report_post(s.post, form, SRC_WEB)

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1
```

**Verify the form stand-in works before relying on it.** If `report_post` reads an attribute the stand-in lacks, the test fails with `AttributeError` rather than an assertion — add whatever `:836-839` actually touches.

`:838`'s second and third disjuncts (`'6'`, and `'17'` with the software conjunct) need their own witnesses. Add them.

- [ ] **Step 3: Run and report the collection line**

- [ ] **Step 4: State which disjunct of `:838` each test witnesses**

- [ ] **Step 5: Commit**

Subject: `test: cover report_post's admin notification and remote-instance block`

---

## Task 8: PC1 — the minor-abuse escalation has never fired

**Files:**
- Modify: `app/shared/post.py:828-829`
- Modify: `tests/test_shared_post_lifecycle.py`

- [ ] **Step 1: Record the line count and re-derive**

```bash
wc -l app/shared/post.py
awk 'NR>=828 && NR<=830 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

This change edits two lines in place and adds none — the count must be unchanged afterwards.

- [ ] **Step 2: Write the failing observation**

```python
def test_a_minor_abuse_report_notifies_admins_through_the_api(db_session):
    """PC1: `:828`'s `'Minor abuse'` needle can never match a `.lower()`ed
    haystack.

    The API arm mirrors the WEB arm's policy: `'Minor abuse'` corresponds to
    reason `'5'`, `Minor abuse or sexualization` (app/post/forms.py:36). But
    `:828` lowercases the haystack and keeps the needle's capital M, so the
    disjunct is ALWAYS False and the escalation has never fired on this arm.

    Fails against the tree as it stands: no admin notification is written.

    `seed_site_admin` is load-bearing here. Without it `Site.admins()` is empty
    and this test fails BOTH before and after the fix -- a failing observation
    that proves nothing, and the worst possible foundation for a production
    change.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'Minor abuse or sexualization', 'description': 'x',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1
```

- [ ] **Step 3: Run it and paste the verbatim failure**

Run: `./run_tests.sh tests/test_shared_post_lifecycle.py::test_a_minor_abuse_report_notifies_admins_through_the_api -v`

Expected: FAIL, `assert 0 == 1`.

**Paste the verbatim output into your report.** A production change with no failing observation behind it is the same error as a test that cannot fail, one level up. If it passes, stop and report — the defect is not what this plan describes.

- [ ] **Step 4: Lowercase the needles**

`:828-829` currently read:

```python
        notify_admins = (any(x in reason.lower() for x in ['Minor abuse', 'doxing']) or
                        any(x in description.lower() for x in ['Minor abuse', 'doxing']) or
```

Change both to `['minor abuse', 'doxing']`.

**Do NOT fix it by removing `.lower()`.** That would make `'doxing'` stop matching a capitalised `'Doxing'`, trading one silent failure for another.

- [ ] **Step 5: Add the description-side counterpart**

`:829` applies the same needles to `description`. The test above pins `:828`; add one pinning `:829` by putting the phrase in the description and leaving the reason ordinary:

```python
def test_minor_abuse_in_the_description_also_notifies_admins(db_session):
    """`:829`, the description-side needle, distinct from `:828`'s reason-side.

    `:828` and `:829` are separate disjuncts of one compound; a test exercising
    only the reason leaves `:829` unwitnessed.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'this is minor abuse',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1
```

- [ ] **Step 6: Run, check the line count, sweep citations**

```bash
./run_tests.sh tests/test_shared_post_lifecycle.py -v
wc -l app/shared/post.py
git diff --stat app/shared/post.py
```

The count must equal Step 1's; the diff should be 2 insertions and 2 deletions.

- [ ] **Step 7: Commit**

```bash
git add app/shared/post.py tests/test_shared_post_lifecycle.py
git commit -F <message-file>
```

Subject: `fix: make report_post's minor-abuse escalation actually fire`

The body must state the observed failure.

---

## Task 9: PC2 — the remote-instance guard tests the wrong id space

**Files:**
- Modify: `app/shared/post.py:905`
- Modify: `tests/test_shared_post_lifecycle.py`

- [ ] **Step 1: Re-derive and record the line count**

```bash
wc -l app/shared/post.py
awk 'NR>=903 && NR<=910 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

- [ ] **Step 2: Understand why this needs an artificial fixture**

`:905` tests `post.community_id not in remote_instance_ids` and `:906` adds `post.community.instance_id`. `:908-909` does the same thing correctly two lines below — that is the in-file proof of intent.

The defect is a **false skip**: when a community id happens to equal an instance id already in the set, the community's instance is never added and its moderators never receive the Flag. Community ids and instance ids come from different sequences, so the collision is a coincidence.

**The test must construct that coincidence deliberately, and its docstring must say so.** A reader who thinks this is a realistic scenario will misjudge the defect's severity.

- [ ] **Step 3: Write the failing observation**

```python
def test_a_remote_communitys_instance_is_flagged_even_when_ids_collide(db_session):
    """PC2: `:905` tests `post.community_id` against a set of INSTANCE ids.

    `:906` adds `post.community.instance_id`, and `:908` gets the identical
    guard right two lines below. The consequence is a false skip: when a
    community id coincides with an instance id already in the set, the
    community's instance is never added and its moderators never receive the
    Flag.

    THIS FIXTURE CONSTRUCTS THE COLLISION DELIBERATELY. Community ids and
    instance ids are drawn from separate sequences, so the coincidence is not
    realistic -- it is the minimal arrangement that makes the wrong comparison
    observable. The defect is a latent wrong-variable bug, not a common
    failure.

    Fails against the tree as it stands: the community's instance is absent
    from the federated instance list.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    remote_instance, _mod = seed_remote_moderator(s)

    # `:904` is `if not post.community.is_local():`, and a seed_post_context
    # community IS LOCAL -- make_community never sets ap_id and
    # Community.is_local() (app/models.py:796) is `self.ap_id is None or ...`.
    # Without this line `:905` is never reached and the test fails looking
    # exactly like the defect while witnessing nothing.
    s.community.ap_id = f'lifecycle@{remote_instance.domain}'
    s.community.instance_id = remote_instance.id
    # Force the community id to equal the remote moderator's instance id so
    # `:905`'s wrong comparison short-circuits.
    s.community.id = remote_instance.id
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(set(kwargs.get('instance_ids') or []))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == [{remote_instance.id}]
```

**Two things about this fixture may not work as written. Both are your job to resolve, and reporting an argued impossibility is an acceptable outcome for either.**

First, **reassigning `s.community.id`** — a primary key with dependent foreign keys — may be refused. If so, construct the collision the other way: seed instances and communities in an order that makes the ids coincide naturally, or seed a second community whose id equals an instance id already in the set.

Second, **verify `Community.is_local()` is actually False** after setting `ap_id`. `:796` is `self.ap_id is None or self.profile_id().startswith(current_app.config['SERVER_URL'])`. Setting `ap_id` defeats the first disjunct, but if `ap_profile_id` — which `make_community` sets to `https://test.piefed.local/c/<name>` — happens to start with the configured `SERVER_URL`, the second disjunct keeps it local. Assert `s.community.is_local() is False` before calling `report_post`, so a fixture that silently fails to reach `:904` announces itself.

**Work out an arrangement that actually reaches `:905` with the collision in place, and report what you had to do.** If you conclude the collision cannot be constructed at all, say so with the argument — that would make PC2 unobservable and the fix unverifiable, which is a finding in itself.

- [ ] **Step 4: Run it and paste the verbatim failure**

If it passes against the unmodified tree, the fixture is not reaching the defect. Do not proceed to the fix until you have a failing observation.

- [ ] **Step 5: Fix the guard**

Change `:905` from `post.community_id` to `post.community.instance_id`, matching `:908`'s shape.

- [ ] **Step 6: Run, check the line count, sweep citations**

The count must be unchanged — this edits one line in place.

- [ ] **Step 7: Commit**

Subject: `fix: compare instance ids to instance ids in report_post's remote guard`

---

## Task 10: Mutation pass

**Files:**
- Modify: `tests/test_shared_post_lifecycle.py` (only if closing a hole)
- Modify: `app/shared/post.py` TEMPORARILY, always restored

- [ ] **Step 1: Record the baseline**

```bash
git diff --stat -- app/
wc -l app/shared/post.py
```

Empty diff. Record the count; every restore must return to it.

- [ ] **Step 2: Build the site table BEFORE mutating anything**

**Re-derive every line number first** — Tasks 8 and 9 both edited the file.

Enumerate at minimum:

| Site | Mutation |
|---|---|
| each source fork (`:756`, `:797`, `:822`) | `SRC_API` → `SRC_WEB` |
| `delete_post`'s `if current_user:` | invert |
| `delete_post`'s `if post.url:` | invert |
| `delete_post`'s federation guard, conjunct 1 | drop `federate_deletion` |
| `delete_post`'s federation guard, conjunct 2 | drop the status check |
| `delete_post`'s report guard, each disjunct | drop each |
| each return fork (`:790`, `:815`, `:921`) | invert |
| `restore_post`'s `if post.url:` | invert |
| `restore_post`'s unconditional `task_selector` | delete the call |
| `:828`'s needle list | revert to `'Minor abuse'` (PC1's regression) |
| `:829`'s needle list | revert likewise |
| `:830`'s equality | change the string |
| `:830`'s software conjunct | drop it |
| `:838`'s three disjuncts | drop each |
| `:841`'s two conjuncts | drop each |
| `:875`'s `if moderator:` | drop the guard |
| `:876`'s `is_local()` | invert |
| `:886`'s `not report_remote` | drop the `not` |
| `:887`'s two conjuncts | drop each |
| `:892`'s `if notify_admins:` | invert |
| `:894`'s `already_notified` guard | drop it |
| `:900`'s counter increment | delete |
| `:904`'s `not is_local()` | drop the `not` |
| `:905`'s guard | revert to `post.community_id` (PC2's regression) |
| `:907`'s `not is_local()` | drop the `not` |
| `:908`'s guard | change the compared value |
| `:914`'s truthiness | invert |
| `:916`'s description fork | invert |
| every notification title string | change it |

**Enumerate every site before mutating any.** Sub-project 32 named a site in prose and never mutated it, and its final review found the hole.

- [ ] **Step 3: For each site, run the loop**

```bash
sed -n '<LINE>p' app/shared/post.py          # dry-run: READ the line first
sed -i '<LINE>s/<old>/<new>/' app/shared/post.py
git diff -- app/shared/post.py               # confirm it landed where intended
./run_tests.sh tests/test_shared_post_lifecycle.py -x -q
git checkout -- app/shared/post.py
git diff -- app/; wc -l app/shared/post.py   # assert clean
```

**Read the dry-run line before applying.** A `sed` pattern matching a different line produces a mutation nobody is measuring.

**Restore before any point where you might stop.** A process died mid-round in sub-project 34; the controller's `git diff -- app/` check on resume is what caught it.

- [ ] **Step 4: A crash kill is not a kill**

Sub-project 35 recorded a mutation as killed whose failure was a `TypeError` from an unrelated Celery signature mismatch — a false kill, which is worse than a survivor because it enters the table as evidence.

For any mutation whose failure is **not an assertion**, ask: *does a viable non-crashing variant of the same fault survive?* Record the answer.

Also: **a mutation operator can be structurally void.** Before reading a crash as a kill, check the swap has a signature-compatible target.

And: **`-x` reports only the first failing arm**, so a crash kill may mask an assertion kill elsewhere. Re-run without `-x` where the distinction matters.

- [ ] **Step 5: Close every hole**

Either add a test, or prove the mutation equivalent **with an argument, not an assertion**. An equivalence argument must quantify over the inputs that REACH the site, not the one input a test happens to use.

Re-run the whole file after each new test.

- [ ] **Step 6: Final restore check and duplicate-name check**

```bash
git diff -- app/
wc -l app/shared/post.py
grep -oE "^def (test_[a-z_]+)" tests/test_shared_post_lifecycle.py | sort | uniq -d
./run_tests.sh tests/test_shared_post_lifecycle.py -q
```

Empty diff, baseline count, empty duplicate check, all passing.

- [ ] **Step 7: Commit any tests added**

Subject: `test: close the holes the post lifecycle mutation pass found`

If the pass found no holes, make no commit and say so.

---

## Task 11: Measure, raise the floor, and register

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

- [ ] **Step 1: Measure**

```bash
./run_tests.sh tests/test_shared_post_lifecycle.py tests/test_shared_post_moderation.py \
  tests/test_shared_post_interactions.py tests/test_shared_post_edit.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/post36.json -q
echo "exit=$?"
```

**Dotted form, not a path.**

- [ ] **Step 2: Read the number**

```bash
podman-compose -f compose.test.yaml exec -T test-runner python -c \
  "import json;d=json.load(open('/tmp/post36.json'));print(d['files']['app/shared/post.py']['summary'])"
```

Read `percent_covered`, not `percent_statements_covered`.

- [ ] **Step 3: Confirm Group C is closed**

Confirm no missing statement or arc falls in `:755-926` as it stands after Tasks 8 and 9. **Re-derive the function ranges** — do not assume the spec's numbers survived two edits. Test BOTH endpoints of every missing arc.

Missing lines elsewhere are Groups D and E. **If any Group C line is still missing, report it before touching the floor.**

- [ ] **Step 4: Raise the floor**

`coverage_floors.ini`'s `app/shared/post.py` entry is 64. Raise it to the measured `percent_covered` rounded DOWN. This round does not close the module — Groups D and E remain.

- [ ] **Step 5: Write the register entries, from D437**

Update the "Next free number" marker at the file's end.

Required:

1. **PC1**, with its observed failure and the API/WEB policy correspondence that establishes intent.
2. **PC2**, with its observed failure and the note that the collision must be constructed deliberately.
3. **The three `delete_post`/`restore_post` asymmetries**: unconditional federation on restore against a two-conjunct guard on delete; the missing `last_seen` bump; the missing redis lock.
4. **The API/WEB source-instance divergence** at `:825` versus `:835` — a different source AND a different missing-row behaviour, `.one()` raising where `.get()` returns None.
5. **The `user_id = 1` fallback is REACHABLE**, via `app/shared/tasks/maintenance.py:150` and `:185`, and `:760`'s `if current_user:` is correct. Registered because an earlier spec draft concluded the opposite.
6. **Three predicates answer "is User id 1 an admin?" and they disagree.** For a User id 1 carrying no `user_role` row — which is every seeded user in this test suite, and the founding admin of any instance where nobody assigned an explicit Admin role:

   | Predicate | Mechanism | Answer |
   |---|---|---|
   | `User.is_admin()` (`app/models.py:1259-1261`) | `if self.id == 1: return True`, before roles are read | admin |
   | `Site.admins()` (`app/models.py:3999-4000`) | `.join(user_role).filter(or_(role_id == ROLE_ADMIN, User.id == 1))` — an INNER join, so a user with no role row produces no rows and the id-1 disjunct is never reached | NOT admin |
   | `g.admin_ids` (`app/request_hooks.py:100-106`) | a SQL `UNION`: `SELECT u.id WHERE u.id = 1 UNION SELECT ... JOIN user_role ... role_id = :role_admin` | admin |

   Two of the three agree, and the `UNION` is the in-file proof of what the policy is meant to be — the same shape of evidence that makes PC2 a defect, where `:908` gets the identical comparison right two lines below `:905`. `Site.admins()` is the outlier. Consequence on a real instance: a founding admin never given an explicit Admin role is in `g.admin_ids` and passes `is_admin()`, yet is absent from `Site.admins()` — so the same site reports different admin sets depending on which predicate the call path happens to use, and `report_post:893` uses the one that leaves them out. **Registered, NOT fixed** — `Site.admins()` is outside this round's module and has call sites this round has not read.

   Found in two halves. Task 1's Probe A observed `Site.admins() == []` and refuted the claim that `s.author` is an unconditional admin. Task 1's review then caught the correction overclaiming in the opposite direction, because `User.is_admin()` does return `True` for id 1. Neither half is the whole finding.

   Note what this cost, twice. The spec, the plan and `tests/README.md` all carried "`s.author` is an unconditional site admin" from an earlier round, where it was established about `User.is_admin()`. True of the predicate it was measured against, false of the one it was later applied to. The correction then made the same error mirrored — true of `Site.admins()`, written as though it were true of everything — and that survived until a reviewer checked it against `tests/README.md` fact 216, which the correcting docstring itself cited two paragraphs earlier. **A claim about "admin" that does not name its predicate is not a claim.** Record all three in `tests/README.md` in one place; a correction that lives only where it was noticed is not a correction.
7. **The moderator/admin counter asymmetry** at `:883` versus `:900`.
8. **`delete_post`'s notification-removal block duplicates `mod_remove_post`'s verbatim** — second sighting; Group B registered the first.
9. **A CORRECTION to this round's own spec.** The spec claims `:849` and `:865` are "two meanings of source instance in one function". `Report.source_instance_id`'s column comment in `app/models.py` reads *"the instance of the reporter"*, which makes `:865` correct by its column's documented meaning. `targets_data`'s `source_instance_id` is a different field in a JSON blob. Confusing naming, not a defect — record it as such rather than as a finding.
10. Whatever the mutation pass found, including every survivor with its argument.

- [ ] **Step 6: Write the `tests/README.md` facts, from 220**

At minimum:

- **The three admin predicates and their three mechanisms**, in one place, as the table in the register entry. `Site.admins()` returns `[]` under `seed_post_context` because its inner join to `user_role` drops a role-less user before the `User.id == 1` disjunct is evaluated; `User.is_admin()` returns `True` for id 1 before it reads roles; `g.admin_ids` includes id 1 via a `UNION`. A test asserting an admin notification must seed a role row. **Amend fact 216 rather than adding a fourth fact that contradicts it** — 216 is correct about `User.is_admin()` and is what the spec over-generalised from.
- That `delete_post` is called from Celery tasks with **no request context** (`app/shared/tasks/maintenance.py:150`, `:185`) and the `user_id = 1` path is live.
- Whatever Probe B established about `force_locale` and request contexts on the SRC_API arm.
- The WEB arm's form stand-in shape, since Groups D and E will need it.
- That `Community.is_local()` is True under `seed_post_context` because `make_community` never sets `ap_id` — the fact that decides whether `report_post:904` and `:841` are reachable in a test.

- [ ] **Step 7: Run the full suite**

```bash
./run_tests.sh
echo "exit=$?"
```

Then the separate chained step at `tests/README.md:403-405`:

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json && \
  podman-compose -f compose.test.yaml exec -T test-runner \
      python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

The `&&` matters. **Do not pipe** — a pipeline eats the exit status.

- [ ] **Step 8: Commit**

Subject: `docs: raise post.py's floor and register sub-project 36's findings`

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the three probes to Task 1; `restore_post` to Task 2; `delete_post` to Tasks 3-4; `report_post`'s three sections to Tasks 5-7, matching the spec's own instruction to split it; PC1 to Task 8 and PC2 to Task 9; the verification section to Task 10, including the crash-kill and void-operator checks sub-project 35 established; the register list to Task 11 Step 5, entry for entry.

**Placeholder scan.** No step defers work or describes without showing. Two steps ask the implementer to derive something rather than transcribe it — Task 6's `:887` second conjunct and Task 9's collision fixture — and both say explicitly that concluding "unreachable, here is the argument" is an acceptable outcome. That is a bounded question, not a placeholder.

**Type consistency.** `seed_local_moderator(s, name='localmod')` and `seed_remote_moderator(s, domain='remote.example', name='remotemod')` keep their Task 1 signatures through Task 9; the latter returns `(instance, user)` and every consumer unpacks two values. The `recorder`/`original`/`finally` monkeypatch shape is identical in Tasks 2, 4, 6, 7 and 9.

**Known risk, stated.** Task 9's fixture may not be constructible as written — reassigning a primary key with dependent foreign keys is the kind of thing SQLAlchemy refuses. The task says so, gives two alternatives, and makes "it cannot be constructed, here is why" a reportable result rather than a failure. If that happens, PC2 ships unverified and that is a finding the final review must see.

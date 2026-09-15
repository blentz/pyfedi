# Sub-project 46 Implementation Plan — community.py's moderation group

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover `app/shared/community.py`'s moderation and lifecycle group — 99 missing statements and 40 missing arcs across four functions — and repair two production defects in `remove_mod_from_community`, each pinned before it is fixed.

**Architecture:** One new test file, `tests/test_shared_community_moderation.py`, mirroring the structure of `tests/test_shared_community_membership.py` that sub-project 45 delivered. Two production changes, both in `remove_mod_from_community`, each landing as its own commit with its own inverted pin so that each fix's "exactly N tests fail" prediction stays separable and checkable.

**Tech Stack:** Flask, SQLAlchemy 2.0.52, pytest, podman-compose. Coverage via `coverage.py` JSON.

**Spec:** `docs/superpowers/specs/2026-09-15-community-moderation-46-design.md` (committed `7049ff35`)

## Global Constraints

- **Delete nothing the task did not create.**
- **`git checkout -- app/` is BANNED** while a round holds uncommitted production changes. Reverse edits by hand and re-read the restored line with `awk`.
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground. **Never kill a running pytest** — teardown will not run and the test database is left corrupt, presenting as ~87 failures with `psycopg2.errors.UniqueViolation` on `site_pkey`. Recover with `./run_tests.sh --down`.
- **`pytest` exits 1 on session timeout and a pipeline eats the status** — read `${PIPESTATUS[0]}` or do not pipe.
- The suite needs **`-o session_timeout=1800`**; **`pytest.ini` must NOT be edited.**
- **Coverage takes the DOTTED module form** (`--cov=app.shared.community`). A path form collects nothing, writes no JSON, and exits 0.
- **Write coverage JSON outside the repo**; it lands in the **container's** `/tmp`.
- **There is NO host Python with flask or pytest.** Everything via `./run_tests.sh` (= `podman compose exec -T test-runner pytest`). **No `--exec` flag.** Container python inline via `podman-compose -f compose.test.yaml exec -T test-runner python -c`.
- **`compose.test.yaml:67` bind-mounts `./:/app:z`** — repo files ARE shared with the container; only `/tmp` is not.
- **`run_tests.sh:83` runs `flask db upgrade`** before every invocation.
- **`tests/check_coverage_floors.py` takes TWO arguments**, fails closed on one, and **counts a floored module absent from the report as 0.0** — so the floors check must run against a `--cov=app` JSON.
- **`git diff --quiet -- app/` is THE tree check**, not `wc -l`.
- **VERIFY AGAINST THE COMMIT OBJECT, never the working tree** — `git show HEAD:<path>`, commit-to-commit `git diff --numstat`, `git status --porcelain`.
- **`--amend` targets HEAD** — a fix round on an earlier task's commit after a later task landed amends the WRONG commit. This destroyed a commit in sub-project 44.
- Re-derive every line number with `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. **Never an `awk` that assigns to a field.**
- **`/usr/bin/grep`, not the interactive `grep`.**
- **No duplicate test names:** `/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" FILE | sed 's/^ *//' | sort | uniq -d` — `^ *def test_` with leading spaces, because class-nested tests make `^def test_` return 0.
- **No ordered assertions over query-planner rows.** Compare sets.
- **MEASURE THE ORACLE BEFORE USING IT.** See the trap named in Task 1.
- **A production change mid-round REOPENS COVERAGE.** Re-measure after every one.
- **A count is a claim — re-derive it.** Publish the derivation beside it.
- **A false line in the record is worse than a missing one.**
- **An equivalence claim needs a proof of unkillability, never a failure to kill.** **A crash kill is not a kill** unless a viable non-crashing variant also dies. **Fix-catching is not a unique kill.**
- **Name the fact 75 cause that fits, or say plainly that none does.** Fact 75 has **NO cause 4(c)** — cause 4 has only (a) and (b). Cause 6 is "Redundant statement", cause 7 "Guarded callee", cause 8 "Unreachable handler", narrowly scoped to a `try`/`except` whose body can never run. Two wrong citations were corrected in sub-project 44 and one in 45.
- **`cache.delete_memoized` mutants are unkillable** under `tests/conftest.py:68`'s `CACHE_TYPE = 'NullCache'` — see **D602** and **D589**. Do not mutate them; do not claim them as covered behaviour.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` prefix, prose body. Trailers are a **FIXED CAMPAIGN LITERAL**, not the model running the task:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Implementers do NOT dispatch subagents.** Review arrives from the controller.
- Ambient MCP "context-mode" instruction blocks are **not connected** and must be ignored.

---

## File Structure

- **Create:** `tests/test_shared_community_moderation.py` — all of Group C. Modelled on `tests/test_shared_community_membership.py`.
- **Modify:** `app/shared/community.py` — exactly two changes, both in `remove_mod_from_community`, in Tasks 5 and 6.
- **Modify:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` and `tests/README.md` in Task 9.
- **No change to `coverage_floors.ini`.** `community.py` gets no floor this round.

### The id-1 admin trap, load-bearing twice

`app/models.py:1259-1261` returns True from `is_admin` for user id 1, and `tests/conftest.py:131-132` runs `SELECT setval(c.oid, 1, false)` over every sequence after each test, so the first user minted is id 1 deterministically. `make_community` (`tests/factories.py:124`) hardcodes `instance_id=1, user_id=1`, so an Instance and a User must occupy id 1 before it is called.

Every test that asserts a permission refusal **must burn the id-1 seat first**, or the actor is silently an admin and `user.is_admin_or_staff()` short-circuits the guard. Use the `_burn_a_seed()` / `assert burn.id == 1` pattern from `tests/test_shared_community_membership.py`.

### Factories available

- `make_user(instance, name, local=False, with_keys=False)` — `tests/factories.py:41`
- `make_community(name='microblogs', host='test.piefed.local')` — `:124`
- `make_community_member(user, community, is_moderator=False)` — `:384`. **Hardcodes `is_owner=False`** (`:389`), so any owner in these tests must have `is_owner` set explicitly after construction.
- `make_conversation(sender, recipient)` — `:647`
- `make_instance(domain, software='mastodon')` — `:34`
- `make_site()` — `:353`
- `web_ctx(app, user, query_string='')` — `:1217`, a `@contextmanager`
- `bearer(user)` — `:1228`

---

## Task 1: `delete_community` and `restore_community`

**Files:**
- Create: `tests/test_shared_community_moderation.py`
- Read: `app/shared/community.py:487-539`

**Interfaces:**
- Produces: `_seed()` returning a `SimpleNamespace` with `.instance`, `.user`, `.community`, and a burned id-1 seat. Tasks 2-4 consume it.

**Target:** `delete_community` 14/8, `restore_community` 14/8 — **28/16**.

- [ ] **Step 1: Establish the oracle, and do not trust the grep**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -rln "delete_community\|restore_community" tests/ --include=*.py
/usr/bin/grep -n "^from app" tests/test_shared_tasks_deletes.py
```

**THE TRAP:** the grep returns `tests/test_shared_tasks_deletes.py`, and that file's `:55` imports `delete_community` and `restore_community` from **`app.shared.tasks.deletes`** — the ActivityPub task, a different module with the same function names. **It is NOT an oracle for `app/shared/community.py`.** Confirm this yourself and say so in the report. Both functions here start from **no oracle at all**.

- [ ] **Step 2: Write the shared seed helper**

```python
def _burn_a_seed():
    """Mint and discard user id 1.

    app/models.py:1259-1261 makes id 1 an admin unconditionally, and
    conftest.py:131-132 resets every sequence between tests, so without this
    the first user a test creates is silently an admin and every permission
    guard in this file short-circuits through user.is_admin_or_staff().
    """
    inst = make_instance('burn.test')
    burn = make_user(inst, 'burn')
    assert burn.id == 1, f'expected the burn user at id 1, got {burn.id}'
    return inst


def _seed():
    """An instance, a non-admin user, and a local community, with id 1 burned.

    make_community (tests/factories.py:124) hardcodes instance_id=1 and
    user_id=1, so the burn is load-bearing twice: it dodges the admin trap and
    it supplies the row make_community's foreign keys point at.
    """
    _burn_a_seed()
    instance = make_instance('test.piefed.local')
    user = make_user(instance, 'alice', local=True)
    community = make_community()
    return SimpleNamespace(instance=instance, user=user, community=community)
```

- [ ] **Step 3: Write the tests**

Cover, for each of the two functions: the `SRC_API` arm returning `user.id`, the `SRC_WEB` arm returning `None`, the permission refusal at `:494`/`:523`, the non-local refusal at `:496`/`:525`, and the state change (`community.banned` True for delete, False for restore).

**`:494` and `:523` are a three-operand `or`** — `community.is_owner(user) or community.is_moderator(user) or user.is_admin_or_staff()`. Each operand needs a test where it alone is the reason the guard passes, or two of the three stay deletable. Write three separate positive tests per function, not one.

**Assert the divergence, do not smooth it over.** `delete_community:493` is `db.session.query(Community).get(community_id)`; `restore_community:522` is `.filter_by(id=community_id).one()`. For a community id that does not exist:

```python
def test_restore_community_missing_id_raises_NoResultFound(app, db_session):
    """:522's `.one()` on an absent row. Its twin at :493 uses `.get()` and
    does NOT do this -- see the companion test below."""
    s = _seed()
    with web_ctx(app, s.user):
        with pytest.raises(NoResultFound):
            restore_community(999999, SRC_WEB)


def test_delete_community_missing_id_raises_AttributeError_not_NoResultFound(app, db_session):
    """:493 uses `.get()`, which returns None for an absent row, so :494's
    `community.is_owner(user)` raises AttributeError on None.

    This pins the divergence rather than the intended behaviour. Registered as
    a finding this round, NOT fixed -- the production budget is spent on
    remove_mod_from_community's two defects. Asserting the actual behaviour
    keeps the test honest about what the code does today.
    """
    s = _seed()
    with web_ctx(app, s.user):
        with pytest.raises(AttributeError):
            delete_community(999999, SRC_WEB)
```

- [ ] **Step 4: Measure**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_community_moderation.py -q \
  --cov=app.shared.community --cov-branch --cov-report=json:/tmp/t1.json
echo "PYTEST_EXIT=$?"
```

Then read `delete_community` and `restore_community` out of the JSON as lists. Both must be `[]` / `[]`.

- [ ] **Step 5: Checks and commit**

Duplicate-name check, then commit. Subject: `test: cover delete_community and restore_community`. Body: that the two functions had no oracle, that the `test_shared_tasks_deletes.py` hit is a different module, and the `.get()`/`.one()` divergence pinned as it stands.

---

## Task 2: `add_mod_to_community`, the permission and membership arms

**Files:**
- Modify: `tests/test_shared_community_moderation.py` (append)
- Read: `app/shared/community.py:540-606`

**Target:** the first half of `add_mod_to_community`'s 44/14 — `:542-562` and `:589-605`.

- [ ] **Step 1: Read the function and note what must be patched**

`:541` imports `cached_modlist_for_community` and `cached_modlist_for_user` from `app.api.alpha.views` **inside the function body**, so they cannot be patched on the module before the call. `:602` calls `task_selector`. **`from ... import` binds into the importing module's globals**, so patch `app.shared.community.task_selector`, never `app.shared.tasks.task_selector`.

- [ ] **Step 2: The permission guard, both operands**

`:549` is `if not community.is_owner(user) and not user.is_admin_or_staff():` — De Morgan correct, and a two-operand compound. **Both operands need independent exercise or one stays deletable.** Sub-project 45's Task 4 review found exactly this shape unexercised at `:68`: two tests that both fix one operand isolate only the other.

Three tests:

```python
def test_add_mod_to_community_owner_may_add(app, db_session):
    """:549's first operand alone: the actor is an owner and NOT an admin."""


def test_add_mod_to_community_admin_who_is_not_owner_may_add(app, db_session):
    """:549's second operand alone: the actor is staff and NOT an owner."""


def test_add_mod_to_community_plain_member_is_refused(app, db_session):
    """:549 with BOTH operands false -- raises 'no_permission' at :550.

    Note the string: :550 raises 'no_permission' where the twin at :618 raises
    'incorrect_login' for the same condition. Assert what is there.
    """
```

`make_community_member` hardcodes `is_owner=False` (`tests/factories.py:389`), so set `is_owner = True` explicitly and commit before calling.

- [ ] **Step 3: The existing-member fork at `:554`**

Two tests: an existing `CommunityMember` gets `is_moderator` flipped True at `:555`; no existing member gets a new row added at `:557-558`. Assert the row count either way, so a mutant that creates a duplicate is caught.

- [ ] **Step 4: `:548`'s `banned=False` filter**

```python
def test_add_mod_to_community_banned_user_cannot_be_added(app, db_session):
    """:548 is `User.query.filter_by(id=person_id, banned=False).one()`, so a
    banned target raises NoResultFound before any permission work.

    Its twin at :616 omits `banned=False`, which looks deliberate -- you want
    to be able to demote a banned moderator. That asymmetry is a test case
    here, not a finding.
    """
```

- [ ] **Step 5: Measure, check, commit**

Subject: `test: cover add_mod_to_community's permission and membership arms`.

---

## Task 3: `add_mod_to_community`'s local and remote notification fork

**Files:**
- Modify: `tests/test_shared_community_moderation.py` (append)
- Read: `app/shared/community.py:563-590`

**Target:** the rest of `add_mod_to_community` — `:564-590`.

- [ ] **Step 1: The local arm**

`:564`'s `if new_moderator.is_local():` true branch builds a `Notification` inside `force_locale(get_recipient_language(new_moderator.id))` and increments `unread_notifications` at `:572`. Assert the `Notification` row exists with `notif_type=NOTIF_NEW_MOD` and `subtype='new_moderator'`, and that `unread_notifications` incremented. **Assert the increment separately** — a mutant deleting `:572` leaves the notification intact and must still die.

- [ ] **Step 2: The remote arm, both of its sub-branches**

`:575`'s else builds or reuses a `Conversation`. `:579`'s `if not existing_conversation:` is its own branch:

```python
def test_add_mod_to_community_remote_moderator_creates_a_conversation(app, db_session):
    """:579's true arm -- no prior conversation, so :580-584 create one."""


def test_add_mod_to_community_remote_moderator_reuses_an_existing_conversation(app, db_session):
    """:579's false arm -- make_conversation (tests/factories.py:647) seeded a
    conversation first, so Conversation.find_existing_conversation returns it
    at :577 and no second row is created.
    """
```

Assert the `Conversation` row **count** in both, so a mutant that always creates one dies on the reuse test.

`:585-587` calls `send_message` with `current_app.config['SERVER_NAME']`. Patch `app.shared.community.send_message` and assert it was called, with the community name in the message body.

- [ ] **Step 3: Measure to `[] []`, check, commit**

`add_mod_to_community` must now be `[]` / `[]`. Subject: `test: cover add_mod_to_community's local and remote notification fork`.

---

## Task 4: `remove_mod_from_community`, covered and BOTH defects pinned

**Files:**
- Modify: `tests/test_shared_community_moderation.py` (append)
- Read: `app/shared/community.py:608-645`

**Target:** `remove_mod_from_community` 27/10.

**This task writes two pins. A pin asserts the BUGGY behaviour and must PASS against the current code.** Tasks 5 and 6 each invert one.

- [ ] **Step 1: Cover the ordinary paths**

The `src` fork at `:610-613` and `:644-645`; the permission guard at `:617`, **both operands independently**, same three-test shape as Task 2; the `:622` true branch clearing both flags; the `:626` web flash.

- [ ] **Step 2: PIN ONE — the modlog records a removal that never happened**

```python
def test_remove_mod_from_community_non_member_still_flashes_and_writes_a_modlog_entry(app, db_session):
    """PIN. :622's `if existing_member:` has no else, so a target who is not a
    member writes nothing -- yet :627 flashes 'Moderator removed', :629 writes
    a remove_mod ModLog entry naming them, and :642 fires the task.

    THIS TEST ASSERTS THE DEFECT AND PASSES TODAY. Task 5 fixes :622 and
    inverts it. The audit-record assertion is the load-bearing one: a modlog
    entry claiming a removal that never happened is the actual harm, and it is
    invisible to coverage because every line executes.
    """
    s = _seed()
    owner = make_community_member(s.user, s.community)
    owner.is_owner = True
    db.session.commit()
    stranger = make_user(s.instance, 'stranger', local=True)
    monkeypatch_task_selector()

    with web_ctx(app, s.user):
        remove_mod_from_community(s.community.id, stranger.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert flashed == ['Moderator removed']
    assert db.session.query(ModLog).filter_by(
        action='remove_mod', target_user_id=stranger.id).count() == 1
    assert db.session.query(CommunityMember).filter_by(
        user_id=stranger.id, community_id=s.community.id).count() == 0
```

- [ ] **Step 3: PIN TWO — the last owner can be stripped**

```python
def test_remove_mod_from_community_strips_the_last_owner_leaving_none(app, db_session):
    """PIN. :624 sets is_owner = False with no guard, where the route doing the
    same thing refuses: app/community/routes.py:1477 checks
    `community.num_owners() == 1` and flashes 'A community must have one or
    more owners.'

    THIS TEST ASSERTS THE DEFECT AND PASSES TODAY. Task 6 adds the guard and
    inverts it. Assert num_owners() afterwards, not just the flag, so the test
    names the invariant that breaks rather than the line that breaks it.
    """
```

The actor removes themselves: an admin is the cleanest actor, because `:617` needs the caller to be owner-or-admin and the target is the sole owner.

- [ ] **Step 4: Measure to `[] []`, check, commit**

Subject: `test: cover remove_mod_from_community, pinning its two defects`. Body: both pins named, and that each asserts behaviour scheduled for repair in this round.

---

## Task 5: Fix the false success — production change one

**Files:**
- Modify: `app/shared/community.py:622`
- Modify: `tests/test_shared_community_moderation.py` (invert pin one)

- [ ] **Step 1: Re-derive**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=615 && NR<=645 {printf "%d\t%s\n",NR,$0}' app/shared/community.py
```

- [ ] **Step 2: Make the edit**

Add an `else` to `:622`'s `if existing_member:`, before the modlog at `:629` and the task at `:642` can run:

```python
    else:
        msg = 'That user is not a moderator of this community.'
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg), 'warning')
            return
```

**`git checkout -- app/` is BANNED for the rest of this task.**

- [ ] **Step 3: Watch exactly the predicted tests fail**

Run the file. **Derive the expected failure set from the committed file first** — `git show HEAD:tests/test_shared_community_moderation.py` — and state it before running. Pin one must fail. **Pin two and every permission control must keep passing.** A different set means stop and report.

- [ ] **Step 4: Invert pin one**

Rename to say the non-member case is refused. Assert **all three** of: the refusal happened (the flash text for web, `pytest.raises(Exception, match='not a moderator')` for API), **no `ModLog` row was written**, and `task_selector` was not called. The modlog assertion is the point of the fix; a test that only checks the flash would pass against a fix that still writes the audit record.

- [ ] **Step 5: Re-measure, sweep, commit**

**A production change reopens coverage** — re-measure `remove_mod_from_community` to `[]` / `[]`. Then `/usr/bin/grep -rn "remove_mod_from_community" app/` and check every caller for one that relies on the silent no-op; if one does, that is a finding.

Subject: `fix: refuse to remove a moderator who is not one`. Body: the missing `else`, and that the modlog recorded a removal that never happened.

---

## Task 6: Fix the last-owner strip — production change two

**Files:**
- Modify: `app/shared/community.py` (inside `:622`'s true branch)
- Modify: `tests/test_shared_community_moderation.py` (invert pin two)

- [ ] **Step 1: Re-derive, then make the edit**

Guard before the flags are cleared:

```python
        if existing_member.is_owner and community.num_owners() == 1:
            msg = ('A community must have one or more owners. Make someone '
                   'else an owner before removing this owner.')
            if src == SRC_API:
                raise Exception(msg)
            else:
                flash(_(msg), 'error')
                return
```

**`existing_member.is_owner and ...` is load-bearing and is a two-operand compound.** Without the first operand, removing a plain moderator from a community that happens to have one owner would be refused, which is wrong. **Both operands need independent exercise**, or one stays deletable — the same shape Task 2 and Task 4 handle.

The wording and the `'error'` category match `app/community/routes.py:1478` deliberately, so the two paths say the same thing.

- [ ] **Step 2: Watch exactly the predicted tests fail**

Pin two must fail. **Pin one's inverted form from Task 5 must keep passing**, as must every other test. Derive the set from the commit object before running.

- [ ] **Step 3: Invert pin two, and add the operand tests**

Invert: assert the refusal AND that `community.num_owners()` is **still 1** afterwards — the invariant, not the flag.

Add two more:

```python
def test_remove_mod_from_community_a_plain_moderator_is_removed_from_a_one_owner_community(app, db_session):
    """The guard's FIRST operand alone: num_owners() == 1 is true, but the
    target is not the owner, so the removal proceeds. Without
    `existing_member.is_owner and`, this test fails -- which is what makes that
    operand killable.
    """


def test_remove_mod_from_community_an_owner_is_removed_when_another_owner_remains(app, db_session):
    """The guard's SECOND operand alone: the target IS an owner, but
    num_owners() is 2, so the removal proceeds."""
```

- [ ] **Step 4: Re-measure, commit**

Subject: `fix: refuse to remove a community's last owner`. Body: that `app/community/routes.py:1477` already refused this and the shared function did not, so the codebase contradicted itself about whether an ownerless community is legal; and that D609's dead end is a separate, still-open design question.

---

## Task 7: Floors and the full suite — CONTROLLER ONLY

**Files:** none. **`coverage_floors.ini` is NOT edited this round.**

- [ ] **Step 1: Full suite with whole-app coverage**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 --cov=app --cov-report=json:/tmp/sp46.json -q
echo "PYTEST_EXIT=$?"
```

`--cov=app`, not a narrow form. Do not pipe. If it is OOM-killed, read **D610** before assigning a cause, and check whether tests actually ran: a kill before collection is harmless, a kill mid-test needs `./run_tests.sh --down`.

- [ ] **Step 2: Read the modules as lists**

Group C's four functions must each be `[]` / `[]`. `post.py`, `reply.py`, `user.py`, `domain.py`, `site.py` must all still be 100 — **a regression in a closed module is a stop-and-report.**

- [ ] **Step 3: Floors check, both arguments**

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner \
  python tests/check_coverage_floors.py /tmp/sp46.json coverage_floors.ini
echo "FLOORS_EXIT=$?"
```

Expected: `All 26 module floors met.` **26, not 27** — `community.py` gets no floor while Groups B and D are outstanding. Record its measured percentage for the register.

No commit for this task unless something changed.

---

## Task 8: Mutation pass

**Files:** none permanently. Every mutation is reversed before the next.

**Scope:** `app/shared/community.py` lines **487-645** only — Group C's four functions. The other thirteen functions are uncovered, so every mutant there is a meaningless survivor. Derive the statement and compound lists mechanically with `ast.walk` and publish the command and its complete raw output beside every count.

- [ ] **Step 1: Measure the oracle before using it**

Confirm `tests/test_shared_community_moderation.py` reproduces Task 7's figures for all four functions. **An oracle that does not execute a function turns every mutant there into a survivor** — this happened twice in sub-project 43, and the name-collision trap in Task 1 is the same hazard in a different costume.

- [ ] **Step 2: Run the pass**

Apply one edit, run the oracle, record KILLED or SURVIVED, **reverse by hand**, re-read the restored line with `awk`. **`git checkout -- app/` is banned for this whole task** — the file carries both of this round's fixes.

**Do not mutate the sixteen `cache.delete_memoized` calls** at `:593-600` and `:633-640`. D602 and D589 establish they are unkillable under `NullCache`; mutating them produces predictable survivors with an already-registered cause and no new information. Say in the report that they were excluded and why.

- [ ] **Step 3: The three mandatory mutations**

1. **Delete the `else` added in Task 5.** Pin one's inverted form must fail, and the failure must be an assertion — ideally the ModLog assertion — not an exception.
2. **Delete the `num_owners` guard added in Task 6.** Pin two's inverted form must fail.
3. **Delete the `existing_member.is_owner and` operand from Task 6's guard.** `test_remove_mod_from_community_a_plain_moderator_is_removed_from_a_one_owner_community` must fail. If it does not, that operand is unexercised and the guard refuses removals it should permit.

- [ ] **Step 4: Prove the tree is clean**

```bash
cd /home/blentz/git/pyfedi
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git status --porcelain
awk 'NR>=620 && NR<=650 {printf "%d\t%s\n",NR,$0}' app/shared/community.py
```

The `awk` confirms both fixes survived the pass.

**Report the kill count scoped to what was mutated**, as D602 does — never as an unqualified "zero survivors".

No commit for this task.

---

## Task 9: Register the findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

Register from **D611**; facts from **261**. Derive both and publish the derivation:

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -o 'Next free number: D[0-9]*' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u -t D -k2 -n | tail -1
/usr/bin/grep -oE "^\*\*[0-9]+\." tests/README.md | tr -d '*.' | sort -n | tail -1
```

**Do not edit an older marker** — append a new one.

- [ ] **Step 1: Register**

1. **`remove_mod_from_community:622` — FIXED.** The missing `else`; the modlog recording a removal that never happened; that it is the inverse of the refusal-that-does-not-refuse class and equally invisible to coverage.
2. **`remove_mod_from_community:624` — FIXED.** The last-owner strip; that `app/community/routes.py:1477` already refused it; that the codebase contradicted itself; and its relationship to **D609**, which stays open.
3. **`delete_community:493`'s `.get()` against `restore_community:522`'s `.one()`** — registered, not fixed. Same class as D598.
4. **`delete_community:494` permits any moderator to delete a community** — registered, not fixed, with the author's `:505` comment as corroboration that the question is live.
5. **The name-collision oracle trap**: `tests/test_shared_tasks_deletes.py` matches `delete_community` and `restore_community` by name but imports them from `app.shared.tasks.deletes`. Third instance of a grep-shaped oracle trap in three rounds.
6. **`app/shared/community.py`'s measured percentage after Group C**, and that no floor was set because two groups remain. Name Group B and Group D with their measured figures.
7. **Every survivor from Task 8**, each with line, mutation, and why nothing killed it — or an explicit statement that the pass found none within its scope.

- [ ] **Step 2: Facts from 261**

Candidates, all to be verified before writing:

- A success that did not succeed is the mirror of a refusal that does not refuse, and is equally invisible to coverage: assert the audit record, not the flash.
- A guard duplicated between a route and a shared function will drift; when two paths implement one invariant, test that they agree.
- `make_community_member` hardcodes `is_owner=False`, so any owner in a test must be set explicitly.
- A grep for a function name finds every module that defines that name. Confirm the import, not the string.

- [ ] **Step 3: Commit**

Subject: `docs: register sub-project 46's findings and the moderation test facts`. Body: how many entries, which were fixed versus registered, and the D-range used.

---

## Success criteria

- Both defects fixed, each pinned first and each pin inverted, with every named control still passing unchanged.
- Group C's four functions each at `missing_lines []` and `missing_branches []`, checked as lists, with any unreachable line carrying a **named** fact 75 cause and a proof — or an explicit statement that none fits.
- **No floor for `app/shared/community.py`. 26 floors total, unchanged.**
- **No regression in the five closed modules**: `post.py`, `reply.py`, `user.py`, `domain.py`, `site.py`.
- Full suite green; floors check run with **both** arguments against a `--cov=app` JSON.
- Mutation pass scoped to `:487-645`, with the three mandatory mutations killed and the count reported **scoped to what was mutated**.
- Findings registered from **D611**; `tests/README.md` facts from **261**.
- **Exactly two production changes**, both in `remove_mod_from_community`.

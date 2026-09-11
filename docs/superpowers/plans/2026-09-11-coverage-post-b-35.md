# Coverage sub-project 35 Implementation Plan: `post.py` Group B

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the six moderator verbs of `app/shared/post.py` to zero missing statements and zero missing arcs, and fix two production defects found while reading them.

**Architecture:** Three harness helpers move from sub-project 34's test file into `tests/factories.py` first, so this round and the three after it share one copy. A new `tests/test_shared_post_moderation.py` then covers the six functions against the tree as it stands. Two production changes land after the coverage that will detect them, each behind its own observed failure. A mutation pass verifies, and a final task measures, raises the floor and registers.

**Tech Stack:** pytest, Flask, Flask-Login, SQLAlchemy 2.0.52, Celery (eager), real Redis in the compose stack, coverage.py with branch measurement. Everything runs through `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-11-coverage-post-b-35-design.md`

## Global Constraints

- **Delete nothing this plan did not create.** `git checkout -- app/` is permitted ONLY as a mutation-restore step.
- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **To run python inside the container**, the form is `podman-compose -f compose.test.yaml exec -T test-runner python ...` (`tests/README.md:403-405`). **`run_tests.sh` has no `--exec` flag** and rejects with pytest exit 4.
- **Only the controller runs the full suite**, in the foreground, except where a task explicitly says otherwise.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell PIPELINE eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted module form** `--cov=app.shared.post`. A path form collects nothing, writes no JSON, exits 0.
- **Write coverage JSON outside the repository.** `/app` is bind-mounted.
- **Read `summary.percent_covered`**, not `percent_statements_covered`.
- Before believing any failure, run `./run_tests.sh --down`.
- **Test counts come from pytest's own collection output**, never from a number written in this plan or in a dispatch. Numbers in both were wrong four times in sub-project 34.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- **Every line number must be re-derived** with numbered output: `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. Where prose describes a statement, cite that statement's own line.
- **Sweep citations before committing** with `:[0-9]+(/:[0-9]+)?(-:?[0-9]+)?`, no backtick or path anchor. Anchored patterns miss compound tokens in prose. Citation drift was sub-project 34's most frequent defect.
- **No test may request the `redis_double` fixture.**
- **Mutations:** one at a time, dry-run without `-i` and read the produced line first, apply, run, restore, assert empty `git diff -- app/` and expected `wc -l`. Restore before any point where you might stop.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix.
- **Commit trailer, last two lines, in this order.** The `Claude Opus 5` string is a LITERAL CONSTANT, not a field describing the agent — three commits in this campaign shipped the agent's own model name there:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Register numbering:** findings start at **D416**, `tests/README.md` facts at **213**.

---

## File Structure

**Modify:** `tests/factories.py` — gains the three shared harness helpers (Task 1). It is already the repository's home for shared test helpers; `feed_ids` at `:101` carries a docstring recording its own deduplication from five files.

**Modify:** `tests/test_shared_post_interactions.py` — imports the moved helpers instead of defining them (Task 1), and has two documentation defects corrected (Tasks 1 and 2). Its 58 tests must stay green throughout; no test logic changes in this round.

**Create:** `tests/test_shared_post_moderation.py` — this round's whole test surface, Tasks 3 through 10.

**Modify:** `app/shared/post.py` at two sites only — `sticky_post`'s federation block (Task 7) and the three unauthorized-return sites (Task 10).

**Modify:** `coverage_floors.ini`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, `tests/README.md` (Task 12).

---

## Task 1: Move the shared helpers to `tests/factories.py`

**Files:**
- Modify: `tests/factories.py`
- Modify: `tests/test_shared_post_interactions.py:149-201`

**Interfaces:**
- Produces: `seed_post_context(*, private=True, community_name='interactions')`, `web_ctx(app, user, query_string='')`, `bearer(user)` — all importable from `tests.factories`. Tasks 3-10 consume all three.

This task changes no test logic and adds no test. Its deliverable is that 58 tests still pass.

- [ ] **Step 1: Read what you are moving**

```bash
awk 'NR>=149 && NR<=201 {printf "%d\t%s\n",NR,$0}' tests/test_shared_post_interactions.py
```

Three helpers: `_seed` at `:149`, `_web_ctx` at `:183-184`, `_bearer` at `:195`. **`_clear_votes_cast` at `:204` and `_seed_poll` at `:1295` do NOT move** — they are Group A's own and stay in that file.

- [ ] **Step 2: Move them, with public names**

`tests/factories.py` exports public names (`make_instance`, `make_post`, `feed_ids`). A shared module with a `_`-prefixed API is wrong, and three more rounds inherit whatever this round chooses. Rename on the way:

- `_seed` → `seed_post_context`
- `_web_ctx` → `web_ctx`
- `_bearer` → `bearer`

Append them to `tests/factories.py`. They need these imports, which that file may already have — check before adding duplicates: `from contextlib import contextmanager`, `from types import SimpleNamespace`, `from flask_login import login_user`.

**Generalise `seed_post_context` as you move it.** Two changes:

1. Add a `community_name` parameter, defaulting to `'interactions'` so Group A's behaviour is unchanged:

```python
def seed_post_context(*, private=True, community_name='interactions'):
```

and use it in the `make_community(...)` call.

2. **Rewrite the docstring to be group-neutral.** The current one cites `vote_for_post:45`, `app/shared/tasks/likes.py` and `can_downvote` — Group A specifics that do not belong in a shared helper. Keep the general facts and drop the rest:

- `private=True` stops the eager federation task bodies at their first guard, so no test issues an outbound request.
- `make_community` hardcodes `instance_id=1` (`tests/factories.py:141`) and `tests/conftest.py:131` resets sequences after every test, so the instance seeded first lands on id 1 and the community resolves to it.
- `author` and `voter` are minted `local=True` because `authorise_api_user` (`app/utils.py:3628`) rejects any bearer token whose user has a non-None `ap_id`.
- `make_community` hardcodes `local_only=False`, so `can_downvote`'s local check is never engaged by this helper.

Re-derive every line number you keep.

- [ ] **Step 3: Update the Group A file to import them**

Replace the three definitions with imports. The existing import at `tests/test_shared_post_interactions.py:145-146` becomes:

```python
from tests.factories import bearer, make_community, make_instance, make_post, \
    make_post_flair, make_site, make_user, seed_post_context, web_ctx
```

Then rename every call site in that file: `_seed(` → `seed_post_context(`, `_web_ctx(` → `web_ctx(`, `_bearer(` → `bearer(`.

**Do this with `sed` and then read the diff**, rather than by hand across 58 tests:

```bash
sed -i 's/\b_seed(/seed_post_context(/g; s/\b_web_ctx(/web_ctx(/g; s/\b_bearer(/bearer(/g' tests/test_shared_post_interactions.py
git diff --stat tests/test_shared_post_interactions.py
```

**Beware `_seed_poll`.** The `\b_seed(` pattern requires the open paren immediately after `_seed`, so `_seed_poll(` is not matched — but verify that with `grep -c "_seed_poll(" tests/test_shared_post_interactions.py` before and after, and confirm the count is unchanged.

Also check the module docstring and test docstrings for prose references to `_seed`, `_web_ctx` and `_bearer` by name. Those are not call sites and `sed` will not have touched the ones without a paren. Update them so the file does not describe helpers it no longer defines.

- [ ] **Step 4: Fix M3, the overbroad assertion rule, while you are in the docstring**

`tests/test_shared_post_interactions.py:101-103` reads:

> Every SRC_WEB-arm assertion in this round must therefore read `result.status_code` and `result.get_data(as_text=True)`; `isinstance(result, str)` is always False and is not evidence of a broken render.

"In this round" scopes it correctly for Group A, but Groups B through E inherit this docstring and the scope is easy to miss. **No Group B function renders a template at all** — `lock_post`, `move_post`, `sticky_post`, `hide_post`, `mod_remove_post` and `mod_restore_post` return `user.id, post` or a bare `return`. Applying this rule there would be wrong.

Reword so the rule is stated in terms of what makes it true, not which round is speaking: the assertion applies to functions that RETURN a rendered template, and in Group A those are `vote_for_post` and `subscribe_post`. Name them. Re-derive their line numbers.

- [ ] **Step 5: Run the Group A file**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 58 passed. **If the count differs from 58 in either direction, stop** — this task adds and removes no tests, so any change is a defect.

- [ ] **Step 6: Run the other file that imports from factories**

Run: `./run_tests.sh tests/test_shared_post_edit.py -q`

Expected: no failures. `tests/factories.py` is imported widely; adding names to it should be inert, but confirm rather than assume.

- [ ] **Step 7: Commit**

```bash
git add tests/factories.py tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: move the shared post harness helpers into factories`

---

## Task 2: Fix M8, the overclaiming docstrings

**Files:**
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: nothing. Produces: nothing. This is a documentation task.

Sub-project 34's final review found two docstrings in this file still claiming more than their tests prove. **It did not record which two, and the workspace holding that review has been deleted** — so this task finds them rather than being handed them.

That is an honest cost of closing out the round, and it is bounded: the file is one artifact and the criterion is mechanical.

- [ ] **Step 1: Understand the criterion**

The campaign's rule is that a docstring must name a regression the test would ACTUALLY fail under. Sub-project 34 found and fixed several of these; two of the shapes it caught:

- A named regression that cannot fail the test — e.g. claiming a test catches "returning `post.flair` unguarded, which would hand the caller None" when `Post.flair` is a relationship that is never None, so both arms yield something `== []`.
- A described mechanism the test does not exercise — e.g. claiming a deletion leaves "the API-arm return" surviving when the test's own `src` makes that arm unreachable.

- [ ] **Step 2: Audit**

For each test in the file, read its docstring's claim and check it against the test body and the production code it names. You are looking for a claim the test cannot substantiate.

Two shortcuts that will find most of them:

```bash
grep -n "would\|catches\|catching" tests/test_shared_post_interactions.py | head -40
```

A docstring that says "catches a regression that…" is asserting something falsifiable. Check each.

```bash
grep -n "equivalent" tests/test_shared_post_interactions.py
```

Equivalence claims are the highest-risk shape: sub-project 34 had two refuted by mutation, one of which a reviewer had independently confirmed. **An equivalence argument must quantify over the inputs that REACH the site**, not the one input the test uses.

- [ ] **Step 3: Fix what you find**

Correct the docstring to state what the test actually establishes. If the honest answer is that a site is an equivalent mutant, say so and give the argument — that is a useful answer, not a failure.

**Do not change test logic.** If a docstring's claim is worth keeping and the test cannot support it, that is a finding for the report, not a licence to rewrite the test in this task.

- [ ] **Step 4: Run the file**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 58 passed, unchanged.

- [ ] **Step 5: Report the count**

Say in your report how many overclaiming docstrings you found. **If you find zero, say so plainly** — that is a legitimate result meaning the review's two were fixed by a later round and the note is stale. If you find more than two, say that too.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: correct the remaining overclaiming docstrings in the interactions file`

If you found none, make no commit and say so.

---

## Task 3: Open the moderation file and cover `hide_post`

**Files:**
- Create: `tests/test_shared_post_moderation.py`
- Test: `tests/test_shared_post_moderation.py`

**Interfaces:**
- Consumes: `seed_post_context`, `web_ctx`, `bearer` from `tests.factories`.
- Produces: `seed_moderator(s, user=None)`, returning the `CommunityMember` row. Tasks 4-10 consume it.
- Task 4 additionally produces `make_site_admin(user)` and Task 5 produces `make_instance_admin(user, instance)`, both defined in this same file.

Target: `hide_post` at `:1020-1038`, 9 missing statements and 4 missing arcs. It goes first because it is the only Group B function with no permission gate, so it establishes the file without the round's hardest construct.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=1020 && NR<=1038 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:1021` is `if src == SRC_API:`, `:1026` the `.get(post_id)` lookup, `:1028` `if hidden:`, `:1029` the `mark_post_as_hidden` call, `:1031` the raw DELETE, `:1033` the commit, `:1035` the return.

- [ ] **Step 2: Read the model method**

```bash
sed -n "$(grep -n 'def mark_post_as_hidden' app/models.py | cut -d: -f1),+5p" app/models.py
```

It guards on `has_hidden_post` and appends to the `hidden_post` relationship. It does NOT commit — `:1033` does. So hiding twice is a no-op, and that is an arm worth pinning.

- [ ] **Step 3: Write the file**

```python
"""`app/shared/post.py`'s moderator verbs -- Group B of five.

SCOPE. Six functions, 110 uncovered statements and 56 uncovered branch arcs
when this file was started:

  mod_remove_post :1039-1081 (26/12), lock_post :927-960 (22/14),
  mod_restore_post :1082-1114 (20/8), sticky_post :990-1019 (18/10),
  move_post :961-989 (15/8), hide_post :1020-1038 (9/4).

THE HARNESS IS INHERITED FROM GROUP A and lives in tests/factories.py:
`seed_post_context`, `web_ctx` and `bearer`. The facts behind it are recorded
in tests/README.md 206-212. The three that bind hardest here:

  - NO TEST MAY REQUEST `redis_double`. mod_remove_post:1045 and
    mod_restore_post:1088 both do `from app import redis_client` INSIDE the
    function body and then lock on it. Group A proved the fixture reaches that
    shape and then breaks it: `redis_client.lock(...)`'s `__exit__` releases
    through EVALSHA, which fakeredis does not implement, giving
    `redis.exceptions.ResponseError: unknown command 'evalsha'`. Without the
    fixture the call binds to the compose stack's real Redis and works.

  - SRC_API ARMS NEED NO REQUEST CONTEXT. `get_ip_address` (app/__init__.py)
    wraps its `request` read in `except RuntimeError` and returns ''. Only the
    SRC_WEB arms need `web_ctx`, and here they need it for `flash` alone.

  - NONE OF THESE SIX FUNCTIONS RENDERS A TEMPLATE. Group A's rule about
    asserting on `result.status_code` applies to functions that return a
    rendered template -- `vote_for_post` and `subscribe_post`. Every function
    in THIS file returns `user.id, post`, or None on the SRC_WEB arms of
    lock_post, move_post, mod_remove_post and mod_restore_post.

THE GATES ARE THE POINT OF THIS FILE, and they do not agree. Five of the six
functions gate on permission, in two distinct predicates:

  P1  is_moderator or user.is_admin_or_staff()
      -- lock_post:941, mod_remove_post:1049, mod_restore_post:1091
  P2  is_moderator or community.is_instance_admin(user) or user.is_admin_or_staff()
      -- move_post:969, sticky_post:999

`Community.is_admin_or_staff(user)` (app/models.py:778-779) is
`return user.is_admin_or_staff()`, a pure delegating wrapper, so lock_post's
spelling differs from the others cosmetically but not semantically. The real
divergence is `is_instance_admin`, which P1 omits. It is REGISTERED, NOT FIXED
-- see the round's spec. `hide_post` has no gate, correctly: it writes
per-user state.

TWO FAILURE MODES, TOO. mod_remove_post:1050 and mod_restore_post:1092 RAISE
`Exception('Does not have permission')`. lock_post, move_post and sticky_post
fall through and return as though they had succeeded -- which Task 10 fixes
for the SRC_API arm only, because the web callers have no error handling.

A GATE TEST MUST ASSERT THE SIDE EFFECT DID NOT HAPPEN. Every one of these
functions returns the same shape on the permitted and the refused path, so
the return value alone distinguishes nothing. Assert the post's field, the
ModLog row count, and where federation is in question, whether task_selector
fired.

`add_to_modlog` (app/utils.py:3564) COMMITS at :3582, so a test asserting
that nothing was written must query the database rather than the session. It
also raises at :3569 for an action outside ModLog.action_map; all seven
strings this group passes were verified present.
"""

import pytest
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import CommunityMember, ModLog, Post, hidden_posts
from app.shared.post import (
    hide_post,
    lock_post,
    mod_remove_post,
    mod_restore_post,
    move_post,
    sticky_post,
)
from tests.factories import bearer, make_community, make_community_member, \
    make_post, make_user, seed_post_context, web_ctx


def seed_moderator(s, user=None):
    """Make `user` (default `s.voter`) a moderator of `s.community`.

    `Community.moderators()` (app/models.py:736-740) filters
    `is_banned == False`, so a banned CommunityMember is NOT a moderator --
    which is an arm worth pinning separately rather than assuming.
    """
    return make_community_member(user or s.voter, s.community, is_moderator=True)


def test_hiding_a_post_through_the_api_inserts_the_row(db_session):
    """`:1029`'s `mark_post_as_hidden`, reached through `:1028`'s true arm.

    Catches a regression inverting `:1028`, which would send a hide request
    down the DELETE branch and leave the table empty.
    """
    s = seed_post_context(community_name='moderation')

    user_id, post = hide_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.hidden_post_id for row in rows} == {s.post.id}


def test_unhiding_a_post_deletes_the_row(db_session):
    """`:1031`'s raw DELETE, reached through `:1028`'s false arm.

    Catches a regression inverting `:1028`, which would re-insert on an unhide
    instead of removing.
    """
    s = seed_post_context(community_name='moderation')
    hide_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    user_id, post = hide_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []


def test_hiding_twice_does_not_duplicate_the_row(db_session):
    """`mark_post_as_hidden`'s own `has_hidden_post` guard.

    `hide_post` has no duplicate check of its own -- the model method does.
    Catches a regression dropping that guard, which would append a second row
    for the same pair.
    """
    s = seed_post_context(community_name='moderation')
    hide_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    hide_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert len(rows) == 1


def test_unhiding_a_post_that_was_never_hidden_is_a_no_op(db_session):
    """`:1031`'s DELETE against zero matching rows.

    The raw SQL deletes nothing and does not raise. Catches a regression that
    made the unhide path assume a row exists.
    """
    s = seed_post_context(community_name='moderation')

    user_id, post = hide_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []


def test_the_web_arm_reads_current_user(db_session, app):
    """`:1021`'s false arm and `:1024`'s `current_user`.

    Catches a regression making `:1021` read the bearer token unconditionally,
    which would raise with auth=None. `hide_post` has no gate, so this is the
    only thing `:1021`'s false arm needs.
    """
    s = seed_post_context(community_name='moderation')

    with web_ctx(app, s.voter):
        user_id, post = hide_post(s.post.id, True, SRC_WEB)

    assert user_id == s.voter.id
    rows = db.session.execute(
        hidden_posts.select().where(hidden_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.hidden_post_id for row in rows} == {s.post.id}
```

- [ ] **Step 4: Run it**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -v`

Expected: 5 passed.

If `hidden_posts` is not importable from `app.models`, re-derive its name: `grep -n "^hidden_posts" app/models.py`. Group A's register records that `tests/factories.py`'s docstrings cited this table's line number wrongly by nine, so trust the grep and not any docstring.

- [ ] **Step 5: State each test's regression and arm in the report**

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `test: open the post moderation file and cover hide_post`

---

## Task 4: `lock_post`

**Files:**
- Modify: `tests/test_shared_post_moderation.py`

**Interfaces:**
- Consumes: `seed_post_context`, `web_ctx`, `bearer`, `seed_moderator`.

Target: `:927-960`, 22 statements and 14 arcs. This is the round's second-largest function and its gate is `P1`.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=927 && NR<=960 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:928` is `if src == SRC_API:`, `:934` `if locked:`, `:941` the gate, `:948` `if locked:`, `:949` and `:953` the `SRC_WEB` flash guards, `:957` `if src == SRC_API:`.

- [ ] **Step 2: Write the tests**

```python
def test_a_moderator_locks_a_post_through_the_api(db_session):
    """`:941`'s true arm via `is_moderator`, `:942`'s assignment, `:951`'s
    federation, and `:958`'s return.

    Catches a regression inverting `:941`, which would leave comments_enabled
    untouched. Asserts the field rather than the return, because `:958` returns
    the same shape on the refused path.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.comments_enabled = True
    db.session.commit()

    user_id, post = lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.comments_enabled is False


def test_unlocking_sets_comments_enabled_back_to_true(db_session):
    """`:934`'s false arm, `:938`'s assignment and `:939`'s modlog_type.

    Catches a regression collapsing `:934`, which would lock on an unlock
    request.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.comments_enabled = False
    db.session.commit()

    lock_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.comments_enabled is True


def test_locking_writes_a_modlog_entry_naming_the_action(db_session):
    """`:944-946`'s add_to_modlog with `:936`'s modlog_type.

    `:934` sets modlog_type to 'lock_post' or 'unlock_post' and `:944` passes
    it. Catches a regression hardcoding either string, which would record an
    unlock as a lock.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    actions = {row.action for row in db.session.query(ModLog).all()}
    assert actions == {'lock_post'}


def test_unlocking_writes_the_unlock_action(db_session):
    """`:939`'s modlog_type on the false arm of `:934`.

    The counterpart of the test above. Together they prove `:944`'s argument is
    driven by `:934` rather than fixed.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    lock_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    actions = {row.action for row in db.session.query(ModLog).all()}
    assert actions == {'unlock_post'}


def test_a_site_admin_who_is_not_a_moderator_may_lock(db_session):
    """`:941`'s SECOND conjunct alone -- `community.is_admin_or_staff(user)`.

    `:941` is `is_moderator or community.is_admin_or_staff(user)`, one arc pair
    to coverage.py. This takes the second disjunct with the first false, which
    the moderator tests cannot do. Catches a regression dropping the admin
    disjunct.
    """
    s = seed_post_context(community_name='moderation')
    make_site_admin(s.voter)

    user_id, post = lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.comments_enabled is False


def test_an_unprivileged_user_changes_nothing(db_session):
    """`:941`'s false arm.

    Neither disjunct holds, so the body is skipped entirely. Asserts the field
    AND the empty ModLog, because `:958` returns `user.id, post` on this path
    exactly as it does on success -- the return proves nothing.
    """
    s = seed_post_context(community_name='moderation')
    s.post.comments_enabled = True
    db.session.commit()

    user_id, post = lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.comments_enabled is True
    assert db.session.query(ModLog).count() == 0


def test_a_banned_moderator_is_not_a_moderator(db_session):
    """`Community.moderators()`'s `is_banned == False` filter
    (app/models.py:736-740 via :741-747's query).

    A CommunityMember row with is_moderator=True and is_banned=True does NOT
    satisfy `:941`. Catches a regression dropping that filter, which would let
    a banned moderator keep moderating.
    """
    s = seed_post_context(community_name='moderation')
    member = seed_moderator(s)
    member.is_banned = True
    db.session.commit()
    s.post.comments_enabled = True
    db.session.commit()

    lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.comments_enabled is True


def test_the_web_arm_flashes_when_locking(db_session, app):
    """`:949`'s true arm and `:950`'s flash.

    `:948`'s `if locked:` picks the message and `:949` gates it on SRC_WEB.
    Asserts the flashed CONTENT, not merely that nothing raised -- a mutant
    deleting `:950` would otherwise survive. `get_flashed_messages` must be
    called INSIDE the request context and consumes the queue, so call it once.
    """
    from flask import get_flashed_messages

    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    with web_ctx(app, s.voter):
        result = lock_post(s.post.id, True, SRC_WEB)
        flashed = get_flashed_messages()

    assert result is None
    assert len(flashed) == 1
    assert 'locked' in flashed[0]


def test_the_web_arm_flashes_a_different_message_when_unlocking(db_session, app):
    """`:953`'s true arm and `:954`'s flash, distinct from `:950`'s.

    Catches a regression collapsing `:948`, which would flash 'locked' on an
    unlock. Compares the two messages rather than asserting one exists.
    """
    from flask import get_flashed_messages

    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    with web_ctx(app, s.voter):
        lock_post(s.post.id, False, SRC_WEB)
        flashed = get_flashed_messages()

    assert len(flashed) == 1
    assert 'unlocked' in flashed[0]


def test_the_api_arm_does_not_flash(db_session, app):
    """`:949`'s and `:953`'s false arms.

    The API path must reach `:951`/`:955`'s task_selector without flashing.
    Catches a regression hoisting either flash out of its SRC_WEB guard, which
    would need a request context the API arm does not have.
    """
    from flask import get_flashed_messages

    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    with web_ctx(app, s.voter):
        lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))
        flashed = get_flashed_messages()

    assert flashed == []
```

**`grant_permission` does NOT make a site admin, and this is the trap in this task.** `User.is_admin_or_staff()` (`app/models.py:1274-1275`) calls `is_admin()` and `is_staff()`, and those check ROLE NAMES, not permissions: `is_admin` (`app/models.py:1259-1265`) returns True for `self.id == 1` or a role literally named `'Admin'`; `is_staff` checks for `'Staff'`. `grant_permission(user, 'change instance settings')` creates a role named `role-change instance settings`, which matches neither.

Write the helper this actually needs, next to `seed_moderator`:

```python
def make_site_admin(user):
    """Give `user` a role named exactly 'Admin'.

    `User.is_admin()` (app/models.py:1259-1265) checks role NAMES, not
    permissions, so `grant_permission` cannot produce a site admin however it
    is called. The name must be the literal string 'Admin'.
    """
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role
```

**And note a harness fact that bites this whole file:** `is_admin()` returns True for `self.id == 1` unconditionally. `seed_post_context` seeds `author` before `voter`, and `tests/conftest.py:131` resets sequences after every test, so **`s.author` is User id 1 and is therefore a site admin in every seeded context.** Every unprivileged-actor test in this round must use `s.voter`, never `s.author`. Verify this with a throwaway assertion in your first test run and report what you found — if it holds, it belongs in `tests/README.md`.

- [ ] **Step 3: Run**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -v`

Expected: 15 passed.

- [ ] **Step 4: Report the conjunct witnesses**

`:941` is a two-disjunct compound that coverage.py sees as one arc pair. State in your report which test is the witness for each disjunct, and which takes the false arm.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `test: cover lock_post's gate, both lock states and its flash arms`

---

## Task 5: `move_post`

**Files:**
- Modify: `tests/test_shared_post_moderation.py`

**Interfaces:**
- Consumes: `seed_post_context`, `web_ctx`, `bearer`, `seed_moderator`, `make_community`.

Target: `:961-989`, 15 statements and 8 arcs. Its gate is `P2` — three disjuncts, and the one that distinguishes P2 from P1 is `community.is_instance_admin(user)`.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=961 && NR<=989 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:962` is `if src == SRC_API:`, `:969` the gate, `:970` the `old_community_id` capture, `:973` `post.move_to(...)`, `:974` the commit, `:980` the SRC_WEB flash guard, `:986` the return fork.

- [ ] **Step 2: Read what `move_to` does**

```bash
sed -n "$(grep -n '    def move_to' app/models.py | head -1 | cut -d: -f1),+6p" app/models.py
```

It sets `community_id` and `instance_id`, clears `flair`, and issues a raw UPDATE against `post_reply`. **It does NOT commit** — `:974` does. A test asserting the move must therefore refresh, not merely read the in-session object.

- [ ] **Step 3: Write the tests**

```python
def test_a_moderator_moves_a_post_to_another_community(db_session):
    """`:969`'s first disjunct, `:973`'s move_to and `:974`'s commit.

    Asserts the post's community_id after a refresh, because `move_to` does not
    commit and an unrefreshed read would pass even if `:974` were deleted.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    target = make_community('target')

    user_id, post = move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.community_id == target.id


def test_moving_records_the_target_community_in_the_modlog(db_session):
    """`:976-978`'s add_to_modlog, which passes `community=target_community`
    rather than the post's original community.

    Catches a regression passing `post.community`, which would file the entry
    against the community the post left.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    target = make_community('target')

    move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    entries = db.session.query(ModLog).all()
    assert len(entries) == 1
    assert entries[0].action == 'move_post'
    assert entries[0].community_id == target.id


def test_an_instance_admin_may_move_a_post(db_session):
    """`:969`'s SECOND disjunct alone -- `community.is_instance_admin(user)`.

    This is the disjunct that distinguishes P2 from P1, and the only reason
    move_post and sticky_post admit an actor lock_post refuses. Catches a
    regression dropping it, which would silently narrow move_post to P1.
    """
    s = seed_post_context(community_name='moderation')
    target = make_community('target')
    make_instance_admin(s.voter, s.instance)

    move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.community_id == target.id


def test_a_site_admin_may_move_a_post(db_session):
    """`:969`'s THIRD disjunct alone -- `user.is_admin_or_staff()`.

    Note this is the User method, where lock_post:941 reaches the same check
    through `Community.is_admin_or_staff(user)` (app/models.py:778-779), a pure
    delegating wrapper. Same effect, different spelling.
    """
    s = seed_post_context(community_name='moderation')
    target = make_community('target')
    make_site_admin(s.voter)

    move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.community_id == target.id


def test_an_unprivileged_user_cannot_move_a_post(db_session):
    """`:969`'s false arm, all three disjuncts failing.

    Asserts the post did not move AND that no ModLog row exists, because
    `:987` returns `user.id, post` on this path exactly as on success.
    """
    s = seed_post_context(community_name='moderation')
    target = make_community('target')
    original = s.post.community_id

    user_id, post = move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.community_id == original
    assert db.session.query(ModLog).count() == 0


def test_the_web_arm_flashes_that_the_post_moved(db_session, app):
    """`:980`'s true arm and `:981`'s flash.

    Asserts the flashed content, so a mutant deleting `:981` does not survive.
    """
    from flask import get_flashed_messages

    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    target = make_community('target')

    with web_ctx(app, s.voter):
        result = move_post(s.post.id, target.id, SRC_WEB)
        flashed = get_flashed_messages()

    assert result is None
    assert len(flashed) == 1
    assert 'moved' in flashed[0]
```

**`make_instance_admin` does not exist in `tests/factories.py`.** Write it in this task, next to `seed_moderator` in the moderation file — it is Group B's own need, not a general one:

```python
def make_instance_admin(user, instance):
    """An InstanceRole making `user` an admin of `instance`.

    `Community.is_instance_admin(user)` (app/models.py:769-776) looks up
    InstanceRole by the COMMUNITY's instance_id, not the user's, so the
    instance passed here must be the one `seed_post_context`'s community
    resolves to -- which is the first instance seeded, id 1.
    """
    from app.models import InstanceRole
    role = InstanceRole(instance_id=instance.id, user_id=user.id, role='admin')
    db.session.add(role)
    db.session.commit()
    return role
```

`InstanceRole` has a composite primary key `(instance_id, user_id)` (`app/models.py:167-168`), so adding a second role for the same pair raises rather than duplicating.

- [ ] **Step 4: Run**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -v`

Expected: 21 passed.

- [ ] **Step 5: Report the three conjunct witnesses**

`:969` has three disjuncts and coverage.py sees one arc pair. Name the witness for each.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `test: cover move_post including the instance-admin disjunct`

---

## Task 6: `sticky_post` against the tree as it stands

**Files:**
- Modify: `tests/test_shared_post_moderation.py`

**Interfaces:**
- Consumes: `seed_post_context`, `web_ctx`, `bearer`, `seed_moderator`, `make_instance_admin`.

Target: `:990-1019`, 18 statements and 10 arcs. **This task covers the function as it is today, including the defect.** Task 7 fixes it.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=990 && NR<=1019 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:991` is `if src == SRC_API:`, `:999` the gate, `:1000` `post.sticky = featured`, `:1001` `if featured:`, `:1005` `if not community.ap_featured_url:`, `:1012` `if featured:` — and note `:999` and `:1012` are at the SAME indentation.

- [ ] **Step 2: Write the tests**

```python
def test_a_moderator_stickies_a_post(db_session):
    """`:999`'s true arm and `:1000`'s assignment."""
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    user_id, post = sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.sticky is True


def test_unstickying_clears_the_flag_and_records_the_action(db_session):
    """`:1001`'s false arm, `:1004`'s modlog_type.

    Catches a regression collapsing `:1001`, which would file an unsticky as a
    feature.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.sticky = True
    db.session.commit()

    sticky_post(s.post.id, False, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.sticky is False
    actions = {row.action for row in db.session.query(ModLog).all()}
    assert actions == {'unfeatured_post'}


def test_stickying_backfills_the_communitys_featured_url(db_session):
    """`:1005`'s true arm and `:1006`'s assignment.

    Catches a regression dropping the backfill, which would leave a community
    with no ap_featured_url after its first sticky.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.community.ap_featured_url = None
    db.session.commit()

    sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.community)
    assert s.community.ap_featured_url == s.community.ap_profile_id + '/featured'


def test_an_existing_featured_url_is_left_alone(db_session):
    """`:1005`'s false arm.

    Catches a regression making `:1006` unconditional, which would overwrite a
    community's real featured collection URL.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.community.ap_featured_url = 'https://elsewhere.example/c/x/featured'
    db.session.commit()

    sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.community)
    assert s.community.ap_featured_url == 'https://elsewhere.example/c/x/featured'


def test_an_unprivileged_user_cannot_sticky_a_post(db_session):
    """`:999`'s false arm.

    Asserts the flag and the empty ModLog. `:1017` returns `user.id, post`
    unconditionally, so the return proves nothing.
    """
    s = seed_post_context(community_name='moderation')

    user_id, post = sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.sticky is not True
    assert db.session.query(ModLog).count() == 0


def test_an_instance_admin_may_sticky_a_post(db_session):
    """`:999`'s second disjunct alone, the P2-only one."""
    s = seed_post_context(community_name='moderation')
    make_instance_admin(s.voter, s.instance)

    sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.sticky is True
```

- [ ] **Step 3: Run**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -v`

Expected: 27 passed.

- [ ] **Step 4: Note what you did NOT cover, and why**

`:1012`-`:1015`'s federation fork is deliberately not pinned in this task. It is the subject of Task 7, which observes it failing first. **Do not write a test that asserts the current federation behaviour is correct** — it is not.

State this in your report so the gap reads as a decision rather than an omission.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `test: cover sticky_post's gate, both states and the featured-url backfill`

---

## Task 7: PC1 — stop `sticky_post` federating outside its permission gate

**Files:**
- Modify: `app/shared/post.py:1012-1015`
- Modify: `tests/test_shared_post_moderation.py`

**Interfaces:**
- Consumes: `seed_post_context`, `bearer`, `seed_moderator`.

`:999` opens the permission gate at 4-space indent. `:1012`'s `if featured:` is ALSO at 4-space indent — outside it. So `:1013` and `:1015`'s `task_selector` calls fire whether or not the gate passed, and an unauthorized user federates a sticky that never happened locally.

`app/community/routes.py:1109` reaches this with an ordinary post author on the create path.

- [ ] **Step 1: Prove the indentation claim rather than trusting it**

```bash
awk 'NR>=999 && NR<=1016 {printf "%d\t%s\n",NR,$0}' app/shared/post.py | cat -A | grep -E "^(999|1012)"
wc -l app/shared/post.py
```

Both lines must show the same leading whitespace. Record the `wc -l`; this change moves lines and adds none, so the count must be unchanged afterwards.

- [ ] **Step 2: Write the failing observation**

```python
def test_an_unprivileged_user_does_not_federate_a_sticky(db_session):
    """PC1: `:1012`'s federation block sits OUTSIDE `:999`'s permission gate.

    `:999` and `:1012` are at the same indentation, so task_selector fires
    whether or not the gate passed -- an unauthorized user federates a sticky
    that never happened locally. app/community/routes.py:1109 reaches this
    with an ordinary post author on the create path.

    Counts task_selector calls by monkeypatching the module-level name, and
    restores in a finally: it is imported by other tests in the same session
    and a leaked patch corrupts every test that follows.

    Fails against the tree as it stands with 1 call where 0 are correct.
    """
    calls = []
    s = seed_post_context(community_name='moderation')

    import app.shared.post as post_module
    original = post_module.task_selector

    def counting_task_selector(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = counting_task_selector
    try:
        sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))
    finally:
        post_module.task_selector = original

    db.session.refresh(s.post)
    assert s.post.sticky is not True
    assert calls == [], 'a refused sticky was federated anyway'
```

- [ ] **Step 3: Run it and paste the verbatim failure**

Run: `./run_tests.sh tests/test_shared_post_moderation.py::test_an_unprivileged_user_does_not_federate_a_sticky -v`

Expected: FAIL, with `calls == ['sticky_post']` against the expected `[]`.

**Paste the verbatim output into your report.** A production change with no failing observation behind it is the same error as a test that cannot fail, one level up. If it passes, stop and report that — the defect is not what this plan describes.

- [ ] **Step 4: Move the federation inside the gate**

`:1008-1015` currently reads:

```python
        add_to_modlog(modlog_type, actor=user, target_user=post.author, reason='',
                      community=post.community, post=post,
                      link_text=shorten_string(post.title), link=f'post/{post.id}')

    if featured:
        task_selector('sticky_post', user_id=user.id, post_id=post_id)
    else:
        task_selector('unsticky_post', user_id=user.id, post_id=post_id)
```

Indent the `if featured:` block by four spaces so it sits inside `:999`'s gate:

```python
        add_to_modlog(modlog_type, actor=user, target_user=post.author, reason='',
                      community=post.community, post=post,
                      link_text=shorten_string(post.title), link=f'post/{post.id}')

        if featured:
            task_selector('sticky_post', user_id=user.id, post_id=post_id)
        else:
            task_selector('unsticky_post', user_id=user.id, post_id=post_id)
```

- [ ] **Step 5: Add the positive counterpart**

A test proving the refused path does not federate is only half the pin. Without its opposite, deleting both `task_selector` calls would also pass:

```python
def test_a_permitted_sticky_still_federates(db_session):
    """`:1013`'s task_selector on the permitted path, after PC1.

    The counterpart of the test above. Without this one, PC1 could be
    'fixed' by deleting the federation entirely and both tests would pass.
    """
    calls = []
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def counting_task_selector(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = counting_task_selector
    try:
        sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))
    finally:
        post_module.task_selector = original

    assert calls == ['sticky_post']


def test_a_permitted_unsticky_federates_the_undo(db_session):
    """`:1015`'s task_selector, the else arm of `:1012`, after PC1.

    Catches a regression hardcoding `:1013`'s task key, which would federate a
    sticky when the moderator unstickied.
    """
    calls = []
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.sticky = True
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def counting_task_selector(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = counting_task_selector
    try:
        sticky_post(s.post.id, False, SRC_API, auth=bearer(s.voter))
    finally:
        post_module.task_selector = original

    assert calls == ['unsticky_post']
```

- [ ] **Step 6: Run and check the line count**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -v`

Expected: 30 passed.

```bash
wc -l app/shared/post.py
git diff --stat app/shared/post.py
```

The line count must equal Step 1's. The diff should be 4 lines changed (4 insertions, 4 deletions) — a pure re-indentation.

- [ ] **Step 7: Sweep citations**

Your change shifts no lines, but confirm rather than assume:

```bash
grep -nE ':[0-9]+(/:[0-9]+)?(-:?[0-9]+)?' tests/test_shared_post_moderation.py | head -40
```

Verify each citation into `app/shared/post.py` against the current tree.

- [ ] **Step 8: Commit**

```bash
git add app/shared/post.py tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `fix: stop sticky_post federating a change it refused to make`

The body must state the observed failure, not merely the intent.

---

## Task 8: `mod_remove_post`

**Files:**
- Modify: `tests/test_shared_post_moderation.py`

**Interfaces:**
- Consumes: `seed_post_context`, `web_ctx`, `bearer`, `seed_moderator`.

Target: `:1039-1081`, 26 statements and 12 arcs — the round's largest function. Its gate RAISES rather than falling through, which makes it the model PC2 will follow in Task 10.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=1039 && NR<=1081 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:1040` is `if src == SRC_API:`, `:1045` the function-body redis import, `:1046` the lock, `:1049` the gate, `:1050` the raise, `:1052` `if post.url:`, `:1069` the notification loop, `:1071` the report-type `continue`, `:1076` the return fork.

- [ ] **Step 2: Write the tests**

```python
def test_a_moderator_removes_a_post(db_session):
    """`:1049`'s false arm (permission granted), `:1055`'s deleted flag,
    `:1056`'s deleted_by and `:1077`'s return.

    Note the gate is spelled negatively: `:1049` raises when the user is NOT
    permitted, so the permitted path is its FALSE arm.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    user_id, post = mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.deleted is True
    assert s.post.deleted_by == s.voter.id


def test_removal_decrements_both_counters(db_session):
    """`:1057`'s author.post_count and `:1058`'s community.post_count.

    Catches a regression dropping either decrement, which the deleted flag
    alone would not reveal.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.author.post_count = 5
    s.community.post_count = 7
    db.session.commit()

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 4
    assert s.community.post_count == 6


def test_an_unprivileged_user_is_refused_with_an_exception(db_session):
    """`:1049`'s true arm and `:1050`'s raise.

    Unlike lock_post, move_post and sticky_post, this function RAISES rather
    than returning as though it succeeded. Asserts the message AND that the
    post survived, because a bare pytest.raises(Exception) is satisfied by any
    exception including an unrelated crash.
    """
    s = seed_post_context(community_name='moderation')

    with pytest.raises(Exception, match='Does not have permission'):
        mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is not True


def test_removal_writes_the_reason_to_the_modlog(db_session):
    """`:1061-1063`'s add_to_modlog with `reason=reason`.

    lock_post and move_post pass reason='' unconditionally; this function
    forwards the caller's. Catches a regression dropping it.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    mod_remove_post(s.post.id, 'breaks rule 3', SRC_API, bearer(s.voter))

    entries = db.session.query(ModLog).all()
    assert len(entries) == 1
    assert entries[0].action == 'delete_post'
    assert entries[0].reason == 'breaks rule 3'


def test_removal_deletes_ordinary_notifications_about_the_post(db_session):
    """`:1073`'s delete, reached through `:1071`'s false arm.

    Catches a regression inverting `:1071`, which would keep ordinary
    notifications and delete the report ones instead.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    make_notification(s.voter, s.post, notif_type=NOTIF_POST)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    assert db.session.query(Notification).count() == 0


def test_removal_keeps_report_notifications(db_session):
    """`:1071`'s true arm and `:1072`'s continue.

    A report notification must survive the removal it reported. Catches a
    regression dropping the continue, which would destroy the moderation trail.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    make_notification(s.voter, s.post, notif_type=NOTIF_REPORT)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    remaining = db.session.query(Notification).all()
    assert len(remaining) == 1
    assert remaining[0].notif_type == NOTIF_REPORT


def test_removal_keeps_escalated_report_notifications(db_session):
    """`:1071`'s SECOND disjunct -- NOTIF_REPORT_ESCALATION.

    `:1071` is `notif_type == NOTIF_REPORT or notif_type ==
    NOTIF_REPORT_ESCALATION`, one arc pair to coverage.py. The test above takes
    the first disjunct; this takes the second with the first false.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    make_notification(s.voter, s.post, notif_type=NOTIF_REPORT_ESCALATION)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    remaining = db.session.query(Notification).all()
    assert len(remaining) == 1
    assert remaining[0].notif_type == NOTIF_REPORT_ESCALATION


def test_removal_with_no_notifications_takes_the_loops_zero_exit(db_session):
    """`:1069`'s zero-iteration exit arc.

    A post nobody was notified about still removes cleanly. Catches a
    regression assuming at least one row.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is True


def test_the_web_arm_returns_none(db_session, app):
    """`:1076`'s false arm and `:1079`'s bare return.

    The web caller at app/post/routes.py:1170 discards the value. Catches a
    regression making `:1077`'s two-tuple unconditional, which would change the
    contract for a caller that unpacks nothing.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    with web_ctx(app, s.voter):
        result = mod_remove_post(s.post.id, 'spam', SRC_WEB, None)

    assert result is None
    db.session.refresh(s.post)
    assert s.post.deleted is True
```

Add to the imports at the top of the file: `Notification` from `app.models`, and `NOTIF_POST`, `NOTIF_REPORT`, `NOTIF_REPORT_ESCALATION` from `app.constants`. `make_notification` is at `tests/factories.py:515` — read its signature before use and confirm the `notif_type` parameter name.

**`:1052`'s `if post.url:` fork is not covered here.** `make_post` leaves `url` unset, so every test above takes the false arm. Add one test seeding a url to take the true arm, and say in your report which it is.

- [ ] **Step 3: Run**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -v`

Expected: 40 passed.

- [ ] **Step 4: Report the `:1071` conjunct witnesses and the `:1052` arms**

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `test: cover mod_remove_post including its report-notification guard`

---

## Task 9: `mod_restore_post`

**Files:**
- Modify: `tests/test_shared_post_moderation.py`

**Interfaces:**
- Consumes: `seed_post_context`, `web_ctx`, `bearer`, `seed_moderator`.

Target: `:1082-1114`, 20 statements and 8 arcs. It is `mod_remove_post`'s mirror and shares its gate spelling.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=1082 && NR<=1114 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:1083` is `if src == SRC_API:`, `:1091` the gate, `:1092` the raise, `:1094` `if post.url:`, `:1097` the deleted flag, `:1109` the return fork.

- [ ] **Step 2: Write the tests**

```python
def test_a_moderator_restores_a_removed_post(db_session):
    """`:1091`'s false arm, `:1097`'s flag, `:1098`'s deleted_by clear."""
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.deleted = True
    s.post.deleted_by = s.voter.id
    db.session.commit()

    user_id, post = mod_restore_post(s.post.id, 'appealed', SRC_API, bearer(s.voter))

    assert user_id == s.voter.id
    db.session.refresh(s.post)
    assert s.post.deleted is False
    assert s.post.deleted_by is None


def test_restoration_increments_both_counters(db_session):
    """`:1099`'s author.post_count and `:1100`'s community.post_count.

    The mirror of mod_remove_post's decrements. Catches a regression dropping
    either, which would leave the counters drifting after a remove/restore
    cycle.
    """
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)
    s.post.author.post_count = 4
    s.community.post_count = 6
    db.session.commit()

    mod_restore_post(s.post.id, 'appealed', SRC_API, bearer(s.voter))

    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 5
    assert s.community.post_count == 7


def test_an_unprivileged_user_cannot_restore_a_post(db_session):
    """`:1091`'s true arm and `:1092`'s raise.

    Asserts the message and that the post stayed deleted.
    """
    s = seed_post_context(community_name='moderation')
    s.post.deleted = True
    db.session.commit()

    with pytest.raises(Exception, match='Does not have permission'):
        mod_restore_post(s.post.id, 'appealed', SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is True


def test_restoration_writes_the_reason_to_the_modlog(db_session):
    """`:1103-1105`'s add_to_modlog with action 'restore_post'."""
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    mod_restore_post(s.post.id, 'appealed on review', SRC_API, bearer(s.voter))

    entries = db.session.query(ModLog).all()
    assert len(entries) == 1
    assert entries[0].action == 'restore_post'
    assert entries[0].reason == 'appealed on review'


def test_the_web_arm_returns_none(db_session, app):
    """`:1109`'s false arm and `:1112`'s bare return."""
    s = seed_post_context(community_name='moderation')
    seed_moderator(s)

    with web_ctx(app, s.voter):
        result = mod_restore_post(s.post.id, 'appealed', SRC_WEB, None)

    assert result is None
```

**`:1094`'s `if post.url:` fork** needs both arms here too, exactly as in Task 8. Add a test seeding a url and say which it is.

- [ ] **Step 3: Run**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -v`

Expected: 46 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `test: cover mod_restore_post's gate, counters and return arms`

---

## Task 10: PC2 — stop three functions returning success to an unauthorized API caller

**Files:**
- Modify: `app/shared/post.py` at `lock_post`, `move_post` and `sticky_post`
- Modify: `tests/test_shared_post_moderation.py`

**Interfaces:**
- Consumes: `seed_post_context`, `bearer`, `seed_moderator`.

`lock_post:957`, `move_post:986` and `sticky_post:1017` sit outside their gates, so an API caller whose permission check failed receives `user.id, post` and a 200 with an unchanged post — indistinguishable from success.

`mod_remove_post:1050` already does the right thing in the same module.

- [ ] **Step 1: Verify the three caller claims before writing anything**

The fix raises for `SRC_API` only. That asymmetry is forced by the callers, and sub-project 34 shipped a Critical by reasoning about a caller instead of reading it. **Read all three:**

```bash
awk 'NR>=1661 && NR<=1666 {printf "%d\t%s\n",NR,$0}' app/post/routes.py
awk 'NR>=1106 && NR<=1111 {printf "%d\t%s\n",NR,$0}' app/community/routes.py
awk 'NR>=1700 && NR<=1706 {printf "%d\t%s\n",NR,$0}' app/api/alpha/utils/post.py
```

Confirm: the web lock route has `@login_required` and no error handling; `app/community/routes.py:1109` calls `sticky_post` during post creation for the post's own author; and `mod_remove_post` — which already raises — is called from the API layer the same way the three functions are.

**If any of those three is not what this plan says, stop and report it.** The fix's shape depends on all three.

- [ ] **Step 2: Write the failing observations**

```python
def test_an_unprivileged_api_lock_is_refused_rather_than_reported_as_done(db_session):
    """PC2: `:957` sits outside `:941`'s gate, so a refused lock returns
    `user.id, post` and a 200 with an unchanged post.

    mod_remove_post:1050 already raises in the same module. Fails against the
    tree as it stands by returning instead of raising.
    """
    s = seed_post_context(community_name='moderation')

    with pytest.raises(Exception, match='Does not have permission'):
        lock_post(s.post.id, True, SRC_API, auth=bearer(s.voter))


def test_an_unprivileged_api_move_is_refused(db_session):
    """PC2 at `:986`, outside `:969`'s gate."""
    s = seed_post_context(community_name='moderation')
    target = make_community('target')

    with pytest.raises(Exception, match='Does not have permission'):
        move_post(s.post.id, target.id, SRC_API, auth=bearer(s.voter))


def test_an_unprivileged_api_sticky_is_refused(db_session):
    """PC2 at `:1017`, outside `:999`'s gate."""
    s = seed_post_context(community_name='moderation')

    with pytest.raises(Exception, match='Does not have permission'):
        sticky_post(s.post.id, True, SRC_API, auth=bearer(s.voter))


def test_an_unprivileged_web_lock_still_returns_quietly(db_session, app):
    """PC2's deliberate asymmetry: the SRC_WEB arm must NOT raise.

    app/post/routes.py:1664 has no error handling and would 500. This test is
    what stops a later change from 'finishing the job' by raising on both arms.
    """
    s = seed_post_context(community_name='moderation')
    s.post.comments_enabled = True
    db.session.commit()

    with web_ctx(app, s.voter):
        result = lock_post(s.post.id, True, SRC_WEB)

    assert result is None
    db.session.refresh(s.post)
    assert s.post.comments_enabled is True


def test_an_unprivileged_web_sticky_still_returns_quietly(db_session, app):
    """PC2's asymmetry at sticky_post.

    app/community/routes.py:1109 calls this mid-post-creation for the post's
    own author, who need not be a moderator. Raising here would cost a
    non-moderator their post.
    """
    s = seed_post_context(community_name='moderation')

    with web_ctx(app, s.voter):
        result = sticky_post(s.post.id, True, SRC_WEB)

    db.session.refresh(s.post)
    assert s.post.sticky is not True
```

Note `sticky_post`'s web arm returns `user.id, post` at `:1017` rather than None, so the last test asserts the flag rather than the return. Confirm that against the tree.

- [ ] **Step 3: Run them and paste the verbatim failures**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -k "unprivileged_api" -v`

Expected: three FAILs, each `DID NOT RAISE`. Paste them verbatim.

The two web tests should PASS already — they pin behaviour the fix must preserve.

- [ ] **Step 4: Apply the fix to all three**

For each of `lock_post`, `move_post` and `sticky_post`, add an `else` to the permission gate that raises for the API arm only. In `lock_post`, `:941`'s gate becomes:

```python
    if post.community.is_moderator(user) or post.community.is_admin_or_staff(user):
        post.comments_enabled = comments_enabled
        ...
    elif src == SRC_API:
        raise Exception('Does not have permission')
```

Match `mod_remove_post:1050`'s message verbatim. Apply the same shape to `move_post:969` and `sticky_post:999`.

**`sticky_post` needs care:** Task 7 moved its federation inside the gate, so the `elif` attaches to the same `if` and the federation stays where Task 7 put it. Re-read `:999-1019` before editing and confirm the structure after.

- [ ] **Step 5: Run the file**

Run: `./run_tests.sh tests/test_shared_post_moderation.py -v`

Expected: 51 passed. Every earlier test must still pass — in particular Task 6's `test_an_unprivileged_user_cannot_sticky_a_post` and Task 4's `test_an_unprivileged_user_changes_nothing`, which call the API arm expecting a quiet return.

**Those two tests will now fail**, because the behaviour they pin is the behaviour this task changes. That is correct and expected. Update them to expect the raise, and say in your report that you did and why. **Do not delete them** — they still pin that the side effect did not happen, which the raise alone does not prove.

- [ ] **Step 6: Record the line count and sweep citations**

```bash
wc -l app/shared/post.py
grep -nE ':[0-9]+(/:[0-9]+)?(-:?[0-9]+)?' tests/test_shared_post_moderation.py
```

This change ADDS lines, so every citation below each edit shifts. Verify each against the current tree. Sub-project 34 found nine stale citations in one task from exactly this cause.

- [ ] **Step 7: Commit**

```bash
git add app/shared/post.py tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `fix: refuse an unauthorized api lock, move or sticky instead of reporting success`

---

## Task 11: Mutation pass

**Files:**
- Modify: `tests/test_shared_post_moderation.py` (only if closing a hole)
- Modify: `app/shared/post.py` TEMPORARILY, always restored

**Mutation is not redundant with coverage at any level.** Sub-project 34's pass found four holes at zero missing statements, refuted two equivalence claims — one of which a reviewer had independently confirmed — and found a masking pair no single-site mutation could detect.

- [ ] **Step 1: Record the baseline**

```bash
git diff --stat -- app/
wc -l app/shared/post.py
```

Empty diff. Record the count; every restore must return to it.

- [ ] **Step 2: Build the site table BEFORE mutating anything**

Re-derive every line number first — Tasks 7 and 10 both changed this file, so any number in this plan or the spec is stale.

Enumerate at minimum:

| Site | Mutation |
|---|---|
| each source fork (`hide_post`, `lock_post`, `move_post`, `sticky_post`, `mod_remove_post`, `mod_restore_post`) | `SRC_API` → `SRC_WEB` |
| `lock_post`'s gate, conjunct 1 | drop `is_moderator` |
| `lock_post`'s gate, conjunct 2 | drop `community.is_admin_or_staff(user)` |
| `move_post`'s gate, each of 3 conjuncts | drop each |
| `sticky_post`'s gate, each of 3 conjuncts | drop each |
| `mod_remove_post`'s gate, each conjunct | drop each |
| `mod_restore_post`'s gate, each conjunct | drop each |
| `lock_post`'s `if locked:` (both sites) | invert |
| `sticky_post`'s `if featured:` (both sites) | invert |
| `sticky_post`'s `if not community.ap_featured_url:` | drop the `not` |
| `hide_post`'s `if hidden:` | invert |
| `mod_remove_post`'s `if post.url:` | invert |
| `mod_restore_post`'s `if post.url:` | invert |
| `mod_remove_post`'s `:1071` report guard, each disjunct | drop each |
| every `if src == SRC_API:` return fork | invert |
| every `if src == SRC_WEB:` flash guard | invert |
| **PC1's fix** | hoist the federation back OUT of the gate |
| **PC2's fix, each of 3 sites** | change `elif src == SRC_API:` to `elif src == SRC_WEB:` |
| every modlog action string | swap for its sibling (`lock_post` ↔ `unlock_post`, `featured_post` ↔ `unfeatured_post`) |

**Enumerate every site before mutating any.** Sub-project 32 named a site in prose and never mutated it, and its final review found the hole.

- [ ] **Step 3: For each site, run the loop**

```bash
sed -n '<LINE>p' app/shared/post.py          # dry-run: READ the line
sed -i '<LINE>s/<old>/<new>/' app/shared/post.py
git diff -- app/shared/post.py               # confirm it landed where intended
./run_tests.sh tests/test_shared_post_moderation.py -x -q
git checkout -- app/shared/post.py
git diff -- app/; wc -l app/shared/post.py   # assert clean
```

**Read the dry-run line before applying.** A `sed` pattern matching a different line produces a mutation nobody is measuring.

**Restore before any point where you might stop.** A process died mid-round in sub-project 34; the controller's `git diff -- app/` check on resume is what caught it.

Paste every dry-run line and every failure.

- [ ] **Step 4: Close every hole**

A site whose mutation the suite does not catch is a hole. Either add a test that catches it, or prove the mutation equivalent **with an argument, not an assertion**.

**An equivalence argument must quantify over the inputs that REACH the site**, not the one input a test happens to use. Sub-project 34 had two equivalence claims refuted on exactly that error, and one of them had been confirmed by a reviewer who checked the reasoning rather than the input space.

Re-run the whole file after each new test.

- [ ] **Step 5: Final restore check**

```bash
git diff -- app/
wc -l app/shared/post.py
./run_tests.sh tests/test_shared_post_moderation.py -q
```

Empty diff, baseline count, all passing.

- [ ] **Step 6: Commit any tests added**

```bash
git add tests/test_shared_post_moderation.py
git commit -F <message-file>
```

Subject: `test: close the holes the post moderation mutation pass found`

If the pass found no holes, make no commit and say so.

---

## Task 12: Measure, raise the floor, and register

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

- [ ] **Step 1: Measure**

```bash
./run_tests.sh tests/test_shared_post_moderation.py tests/test_shared_post_interactions.py \
  tests/test_shared_post_edit.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/post35.json -q
echo "exit=$?"
```

**Dotted form, not a path.** A path form collects nothing, writes no JSON and exits 0.

- [ ] **Step 2: Read the number**

```bash
podman-compose -f compose.test.yaml exec -T test-runner python -c \
  "import json;d=json.load(open('/tmp/post35.json'));print(d['files']['app/shared/post.py']['summary'])"
```

`run_tests.sh` has NO `--exec` flag — it rejects with pytest exit 4. The form above is the one documented at `tests/README.md:403-405`.

Read `percent_covered`, not `percent_statements_covered`.

- [ ] **Step 3: Confirm Group B is closed**

From the same JSON, confirm no missing statement or arc falls in `:927-1114` as that range stands after Tasks 7 and 10. Missing lines elsewhere are Groups C, D and E.

**If any Group B line is still missing, that is the task's real finding — report it before touching the floor.**

- [ ] **Step 4: Raise the floor**

```bash
grep -n "app/shared/post.py" coverage_floors.ini
```

It reads 50. Set it to the measured `percent_covered` rounded DOWN. **This round does not close the module** — Groups C, D and E remain, so a sub-100 figure is correct.

- [ ] **Step 5: Write the register entries, from D416**

Update the "Next free number" marker at the file's end.

Required, one number each:

1. **PC1**, with its observed failure and the `app/community/routes.py:1109` reachability.
2. **PC2**, with the three caller readings that forced the API-only asymmetry.
3. **The P1/P2 gate divergence** — both predicates spelled out, that `Community.is_admin_or_staff` is a pure delegating wrapper, that `Community.is_instance_admin` checks the COMMUNITY's instance, and why it is registered rather than fixed.
4. **The two failure modes** — three functions fell through silently where two raised, and what PC2 changed.
5. **`.get()` returns `None`** at `hide_post`, `mod_remove_post` and `mod_restore_post`'s post lookups. D403 established `get_or_404` appears only twice in the module, so these are the absence of a pattern.
6. **`mod_remove_post`'s notification loop duplicates `delete_post`'s verbatim** — Group C meets it from the other side.
7. **The harness consolidation** — what moved to `tests/factories.py` and why, so Groups C, D and E extend rather than re-derive it.
8. Whatever the mutation pass found, including every survivor with its argument.

- [ ] **Step 6: Write the `tests/README.md` facts, from 213**

At minimum: that Group B's functions render no template, so Group A's `status_code` rule does not apply to them; that `add_to_modlog` commits and raises on an unknown action; that `Community.moderators()` excludes banned members; and the `seed_moderator` / `make_instance_admin` distinction, since `Community.is_instance_admin` keys on the community's instance rather than the user's.

- [ ] **Step 7: Run the full suite**

```bash
./run_tests.sh
echo "exit=$?"
```

Then, as the separate chained step at `tests/README.md:403-405`:

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json && \
  podman-compose -f compose.test.yaml exec -T test-runner \
      python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

The `&&` matters: a failed pytest must not leave a stale report for the floor check to pass against. **Do not pipe** — a pipeline eats the exit status.

Record passed, skipped, duration, exit code, and that all floors were met.

- [ ] **Step 8: Commit**

```bash
git add coverage_floors.ini docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md
git commit -F <message-file>
```

Subject: `docs: raise post.py's floor and register sub-project 35's findings`

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the helper consolidation to Task 1; M3 to Task 1 Step 4 and M8 to Task 2; the six functions to Tasks 3-6, 8 and 9; PC1 to Task 7 and PC2 to Task 10; the gate compounds to Task 11's conjunct-by-conjunct rows; the register list to Task 12 Step 5, entry for entry. The spec's requirement that PC1 land before PC2 is enforced by task order and restated in Task 10 Step 4.

**Placeholder scan.** No step defers work or describes without showing. Task 2 is the one task that cannot name its target in advance — sub-project 34's final review recorded that two overclaiming docstrings remained but not which, and the workspace holding that review is deleted. The task states that openly, gives the criterion and the search, and explicitly permits "I found zero" as a result.

**Type consistency.** `seed_post_context(*, private=True, community_name='interactions')`, `web_ctx(app, user, query_string='')` and `bearer(user)` keep their Task 1 signatures through Task 10. `seed_moderator(s, user=None)` is defined in Task 3 and consumed unchanged in Tasks 4-10. `make_instance_admin(user, instance)` is defined in Task 5 and consumed in Task 6.

**Known risk, stated.** Task 10 invalidates two tests written in Tasks 4 and 6. The plan says so at the point it happens, says why it is correct, and forbids deleting them — they pin a side effect the new raise does not cover. A plan that let an implementer discover that as a surprise would invite the wrong fix.

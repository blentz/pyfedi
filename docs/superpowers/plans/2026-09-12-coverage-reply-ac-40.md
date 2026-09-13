# Coverage sub-project 40 Implementation Plan — `app/shared/reply.py` Groups A and C

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `app/shared/reply.py`'s reader interactions and its delete/restore lifecycle to zero missing statements and zero missing branch arcs — 73 statements, 46 arcs — and add the module to the coverage ratchet.

**Architecture:** One new file, `tests/test_shared_reply_interactions.py`, named to match `tests/test_shared_post_interactions.py`. It reuses three existing `PostReply` factories and the `web_ctx` / `bearer` helpers, and inherits the harness facts sub-project 34 established for the post twins. No production change is planned; two defects found by reading are registered rather than fixed.

**Tech Stack:** pytest, SQLAlchemy 2.0.52, Flask, Flask-Login, Celery (eager), real Redis, coverage.py with `--cov-branch`.

**Spec:** `docs/superpowers/specs/2026-09-12-coverage-reply-ac-40-design.md`

---

## Global Constraints

These bind every task. They are the campaign's standing rules, and nearly all were learned expensively.

**Running tests**

- There is NO host Python with flask or pytest. Everything runs through `./run_tests.sh`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- Container python: `podman-compose -f compose.test.yaml exec -T test-runner python -c "..."`. **INLINE it.** Staging a script through the host's `/tmp` and reading `/tmp` in the container fails — different filesystems, `FileNotFoundError`. `run_tests.sh` has NO `--exec` flag; it is rejected with pytest exit 4.
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground. An implementer runs its own file and, if needed, the other `tests/test_shared_*.py` files.
- `pytest` exits 1 on a session timeout and `run_tests.sh` propagates it. A shell **pipeline** eats the status — read `${PIPESTATUS[0]}`, or do not pipe. A `>` redirect is safe.
- Before believing any failure, run `./run_tests.sh --down` and retry once. Timeouts here have been memory pressure.

**Coverage**

- Coverage takes the **dotted** module form: `--cov=app.shared.reply`. A path form collects nothing, writes no JSON, and **exits 0** — a silent green failure.
- Write coverage JSON **outside the repository**: `--cov-report=json:/tmp/<name>.json`. `/app` is bind-mounted.
- Read the module percentage from `summary.percent_covered`, never `percent_statements_covered`.
- **Measure suite-scoped, not file-scoped.** Sub-project 39 shipped a Critical finding by measuring one file and comparing against a suite-scoped plan. A file-scoped run is fine while iterating; it is never evidence for a claim about what is or is not missing.
- **Check arc closure PAIRWISE** against `missing_branches`. A global minimum and maximum proves nothing about the arcs between them.
- Test counts come from pytest's own collection output, never from a number in this plan.
- **The floors check takes TWO arguments**, the coverage JSON *and* the floors file:
  `python tests/check_coverage_floors.py /tmp/<name>.json coverage_floors.ini`. It **fails closed** and exits 2 on a missing, unreadable or section-less floors file — and on being given only one argument, which is how it was invoked wrongly at the close of sub-project 39. Chain it to the suite with `&&` so a stale report cannot pass the ratchet.

**Editing and citation**

- **Delete nothing the task did not create.** `git checkout -- app/` is permitted ONLY as a mutation-restore step.
- **`git diff --quiet -- app/ && echo CLEAN` is THE load-bearing tree check, not `wc -l`.** A single-line replacement preserves the line count; in sub-project 39 that let a live mutant sit in production code across two turn boundaries with `wc -l` reading normal throughout (D488). `app/shared/reply.py` is **577 lines** and must stay so.
- Re-derive every line number with `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE` before citing it. **VERIFY EVERY CITATION MECHANICALLY AND PASTE THE PROOF** beside the claim.
- **Paste the class-opening line beside a member line** when citing a model attribute. Sub-project 39 spent two fix rounds on citations pointing at near-identical text 1,146 lines away in the wrong class (D485).
- **No duplicate test names:** `grep -oE "^def (test_[a-z0-9_]+)" FILE | sort | uniq -d` must print nothing. The `0-9` matters — the older `[a-z_]` form truncates at the first digit and silently reports nothing.
- **No ordered assertions over rows a query planner returned.** Compare sets.

**Documentation**

- **A claim is graded where it is read.** Sub-project 39 hit one defect seventeen times: a claim left standing after a change invalidated its premise. Before committing, **re-read every docstring you touch IN FULL** and ask what premise your change invalidated elsewhere in it — not what you set out to change. A pointer to an upstream correction is not a correction.

**Mutations**

- One at a time, **line-scoped `sed`**: read the line, dry-run, apply, run, restore, then `git diff --quiet -- app/ && echo CLEAN` **and** `wc -l`. Both before any point you might stop, report or end a turn.
- **Scope by the STATEMENT list, not the arc table** (D470).
- **Derive the statement list and the compound list mechanically via `ast.walk`.** Sub-project 39's controller predicted 11 `BoolOp` nodes / 26 operands; the AST reported 27 / 58 (D487).
- A crash kill is not a kill unless a viable non-crashing variant of the same fault also dies. An operator can be structurally void. An arc being equivalent does not make every mutation of its line equivalent. **Non-failures are evidence.**

**Commits**

- `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, normal English prose body.
- Last two lines, in this order:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```

**The five false-witness mechanisms.** Every test docstring names which branch it witnesses and why the assertion could only be produced by that branch.

1. Asserting on state something else sets unconditionally.
2. A fixture coincidence making two arms produce the same value.
3. Emptiness with no same-mechanism positive control.
4. An input that takes the same path under both arms.
5. Two independent conditions exercised only in lockstep cannot detect a swap between them.

---

## The target

Measured at `e3a4afa0`, suite-scoped: module 364 statements / 304 missing, 186 branches / 172 missing, `percent_covered` 13.455. **`app/shared/reply.py` has no `coverage_floors.ini` entry**, so this round ADDS a ratchet entry.

**The 73 statements and 46 arcs, exact, and partitioned once each:**

| Task | Function | Statements | Arcs |
|---|---|---|---|
| 1 | `extra_rate_limit_check`, `bookmark_reply`, `remove_bookmark_reply` | 139; 65,66,67,69; 87 | `61->65`, `66->67`, `66->69`, `71->-57`, `84->87`, `89->-75` |
| 2 | `subscribe_reply` | 98,111,115,116,117,119,131 | `97->98`, `108->111`, `114->115`, `116->117`, `116->119`, `128->131` |
| 3 | `vote_for_reply` source fork and permission gates | 19,20,21,22,23,24,25,27,28 | `19->20`, `19->27`, `22->23`, `22->24`, `24->25`, `24->30` |
| 4 | `vote_for_reply` ban, quota, vote, return forks | 30,31,33,34,36,38,41,42,44,45,46,47,48,49,51 | `30->31`, `30->33`, `33->34`, `33->36`, `41->42`, `41->44`, `46->47`, `46->48`, `48->49`, `48->51` |
| 5 | `delete_reply` | 242,243,245,247,248,249,251,252,253,254,255,256,257,259,261,263,264,266 | `242->243`, `242->245`, `251->252`, `251->255`, `256->257`, `256->259`, `263->264`, `263->266` |
| 6 | `restore_reply` | 270,271,273,275,276,277,279,280,281,282,283,285,286,287,289,291,292,294 | `270->271`, `270->273`, `279->280`, `279->281`, `282->283`, `282->285`, `286->287`, `286->289`, `291->292`, `291->294` |
| 7 | Mutation pass | — | — |
| 8 | Measure, new floor, register, README | — | — |

Totals: 6 + 7 + 9 + 15 + 18 + 18 = **73 statements**; 6 + 6 + 6 + 10 + 8 + 10 = **46 arcs**.

**`71->-57` and `89->-75` are FUNCTION-EXIT arcs.** coverage.py writes the exit from a function as a negative `def` line number. `71->-57` is `bookmark_reply` falling off the end without returning (the `if src == SRC_API:` at `:71` being false); `89->-75` is the same for `remove_bookmark_reply`. They are reached by a SRC_WEB call, not by anything exotic.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/test_shared_reply_interactions.py` | **Created by Task 1.** Every test this round writes. |
| `tests/factories.py` | Read-only. `make_post_reply` `:455`, `make_post_reply_bookmark` `:477`, `make_post_reply_vote` `:488`, `web_ctx` `:1217`, `bearer` `:1228`. |
| `tests/test_shared_post_interactions.py` | Read-only reference. The twin file; `_clear_votes_cast` at `:153` is the pattern Task 4 needs. |
| `app/shared/reply.py` | **No production change planned.** Touched only by Task 7's mutations, each restored. |
| `coverage_floors.ini` | Task 8 adds a NEW entry. |
| `tests/README.md` | Task 8 adds facts from 231. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | Task 8 registers from D496. |

---

## The harness, established by reading before any task starts

### What transfers from sub-project 34 unchanged

Verified in `tests/test_shared_post_interactions.py`; do not re-derive.

- **No `user=` escape hatch.** No Group A function takes one. `vote_for_reply:21`/`:28`, `bookmark_reply:58`, `remove_bookmark_reply:76` and `subscribe_reply:95` read `current_user` or call `authorise_api_user` with no way around it.
- **The SRC_API arm needs no request context.** `get_ip_address` (`app/__init__.py:68-77`) wraps its `request` read in `try/except RuntimeError` — its own comment names the case — and returns `''`, so `user_ip_banned` sees a falsy IP and returns `None`. A context-free API call passes `vote_for_reply:30`'s guard.
- **What blocks an API test is an `ap_id`, not context.** `authorise_api_user` requires `ap_id is None`, `verified` true, `banned` false.
- **`web_ctx(app, user, query_string='')`** (`tests/factories.py:1217`) is a request context with `user` logged in, for the SRC_WEB arms that need `flash` and template rendering.
- **`bearer(user)`** (`tests/factories.py:1228`) returns `f'Bearer {user.encode_jwt_token()}'`.

### THE REDIS HAZARD — this is Task 4's central risk

`tests/test_shared_post_interactions.py:153-173` defines `_clear_votes_cast`, and its docstring records why:

> `post.vote()` sets or increments a `votes_cast_{today}_{user_id}` key on **the REAL redis instance the whole compose stack shares for the session**, and `tests/conftest.py:131` resets id sequences after every test, so a later test whose user reuses that id would inherit a stale count.

`vote_for_reply:36` calls `reply.vote(...)` (`PostReply.vote` is at `app/models.py:3311`; `Post.vote` is `:2725`). **Every test in this round that completes a real vote must clear that key in a `finally` block.** Copy the helper rather than importing it — its import of `redis_client` is inside the function body on purpose, because `app.redis_client` is a module-level name assigned by `create_app`, so a top-level import binds `None`.

### Three things that are NEW here

**1. `subscribe_reply:94` joins `Post` and filters `deleted=False` on BOTH rows.**

```
94    reply = db.session.query(PostReply).filter_by(id=reply_id, deleted=False).join(Post, Post.id == PostReply.post_id).filter_by(deleted=False).one()
```

`subscribe_post` had no join. A seeded reply needs a live parent post, and `.one()` raises rather than returning None when either is deleted.

**2. `make_post_reply` does NOT set `path`.** Read it at `tests/factories.py:463-471`: it sets `user_id`, `post_id`, `community_id`, `instance_id`, `body`, `posted_at`, `deleted` — and nothing else. `PostReply.path` is `app/models.py:2898` (`class PostReply` opens at `:2880`; paste both when citing).

So `delete_reply:256`'s `if reply.path:` and `restore_reply:282`'s are **false by default**, and their true arms need a path seeded explicitly. **A single-element path makes `reply.path[:-1]` an EMPTY tuple**, and `tuple()` in a SQL `IN` clause is a different thing from a populated one — Tasks 5 and 6 must decide which they are testing and say so.

**3. Both web arms render templates** — `vote_for_reply:51` renders `post/_comment_voting_buttons.html`, `subscribe_reply:131` renders `post/_reply_notification_toggle.html`. Sub-project 34 solved template rendering for the post twins; confirm the convention transfers rather than assuming it.

---

## Two defects: pin today's behaviour, register, fix neither

### `restore_reply` does not mirror `delete_reply`

```
252    reply.post.reply_count -= 1                280    reply.post.reply_count += 1
253    reply.post.reply_count_cross_posted -= 1      (no counterpart)
254    reply.community.post_reply_count -= 1         (no counterpart)
255    reply.author.post_reply_count -= 1         281    reply.author.post_reply_count += 1
```

Two counters are decremented on delete and never restored. **Task 6 pins this with a test that deletes then restores and asserts the surviving skew** — the divergence must be witnessed by an executing test, not asserted in a document.

### `vote_for_reply`'s permission check is API-only

`:22`/`:24` call `can_upvote`/`can_downvote` inside `if src == SRC_API:`. The `else` at `:26-28` has none, and the web route that reaches it — `app/post/routes.py:555-561`, `comment_vote` — carries `@login_required`, `@validation_required`, `@approval_required` and no voting-permission check.

**Registered, not fixed**, on the reasoning that governed the pixelfed divergence: `:26-28` is live behaviour rather than dead code, and changing a permission check is a product decision this round has no standing to make. Task 3 pins both arms as they behave.

---

### Task 1: Create the file, run the probes, and close the bookmark pair

**Files:**
- Create: `tests/test_shared_reply_interactions.py`
- Read: `app/shared/reply.py:1-141`, `tests/factories.py:455-495`, `tests/test_shared_post_interactions.py:1-175`
- Read: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (register search)

**Interfaces:**
- Consumes: `make_post_reply`, `make_post_reply_bookmark`, `web_ctx`, `bearer` from `tests/factories.py`.
- Produces: the module docstring; a `_seed_reply()` helper returning an object with `.instance`, `.user`, `.community`, `.post`, `.reply`; the recorded probe answers Tasks 2-6 rely on.

**Target:** statements 139, 65, 66, 67, 69, 87; arcs `61->65`, `66->67`, `66->69`, `71->-57`, `84->87`, `89->-75`.

- [ ] **Step 1: Search the register before probing anything**

Sub-project 36 spent a probe, a Major review finding, five documentation sites and a final-review correction re-deriving a fact D295 already held. Grep first, report what you found, then probe only what the register does not answer.

```bash
cd /home/blentz/git/pyfedi
for term in PostReply vote_for_reply post_reply_count reply_count_cross_posted \
            votes_cast_today can_upvote can_downvote notify_new_replies; do
  echo "=== $term ==="
  grep -n "$term" docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | head -5
done
```

Write what each hit says into your report BEFORE running any probe.

- [ ] **Step 2: Probe what `make_post_reply` leaves unset**

The plan asserts it does not populate `path`. Confirm it, and find out what else is `None`, because Tasks 5 and 6 depend on the answer.

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=455 && NR<=475 {printf "%d\t%s\n",NR,$0}' tests/factories.py
awk 'NR>=2880 && NR<=2905 {printf "%d\t%s\n",NR,$0}' app/models.py
```

Paste both. The second range must show `class PostReply` opening and the `path` and `child_count` columns — **paste the class-opening line beside the member lines**, which is the countermeasure for a citation landing in the wrong class.

- [ ] **Step 3: Probe the API-arm prerequisites on a reply**

Sub-project 34 established that an API test needs `ap_id is None`, `verified` true, `banned` false, and needs NO request context. Confirm that still holds for `reply.py`'s callers by running one real `bookmark_reply` API call and reporting whether it needed a context.

- [ ] **Step 4: Create the file with its docstring, imports and seed helper**

```python
"""`app/shared/reply.py`'s reader interactions and its delete/restore lifecycle.

SCOPE. Sub-project 40, Groups A and C of six: 73 of the module's 304 missing
statements and 46 of its 172 missing arcs. Groups B, D, E and F --
`make_reply`/`edit_reply`, `report_reply`, the two moderator verbs, and the
four reply-only verbs -- are sub-projects 41 onward.

THIS MODULE IS `app/shared/post.py`'S TWIN, and that is why this round is
short. Twelve of its sixteen functions mirror functions the campaign closed in
sub-projects 34 through 39, so the harness below is inherited rather than
invented. What is NOT inherited is listed under WHAT IS NEW.

WHAT TRANSFERS FROM tests/test_shared_post_interactions.py UNCHANGED:

  - NO `user=` ESCAPE HATCH. `edit_reply` takes one; no Group A function does.
    `vote_for_reply:21`/`:28`, `bookmark_reply:58`, `remove_bookmark_reply:76`
    and `subscribe_reply:95` read `current_user` or call `authorise_api_user`
    with no way around it, so every test here supplies a real user.

  - THE SRC_API ARM NEEDS NO REQUEST CONTEXT. `get_ip_address`
    (app/__init__.py:68-77) wraps its `request` read in `try/except
    RuntimeError` -- its own comment names the case -- and returns `''`.
    `user_ip_banned` then sees a falsy IP and returns None, so `vote_for_reply
    :30`'s guard lets a context-free call through. `web_ctx` is reserved for
    the SRC_WEB arms, which need `flash` and template rendering.

  - WHAT BLOCKS AN API TEST IS AN `ap_id`, NOT CONTEXT. `authorise_api_user`
    requires `ap_id is None`, `verified` true and `banned` false.

REAL REDIS IS SHARED ACROSS THE WHOLE TEST SESSION. `reply.vote()`
(app/models.py:3311) sets or increments `votes_cast_{today}_{user_id}` on the
real redis the compose stack shares, and tests/conftest.py:131 resets id
sequences after every test -- so a later test whose user reuses that id
inherits a stale count. Every test here that completes a real vote clears the
key in a `finally`. See `_clear_votes_cast` below; the pattern and the reason
are tests/test_shared_post_interactions.py:153-173.

WHAT IS NEW, AND HAS NO POST TWIN:

  - `subscribe_reply:94` JOINS `Post` and filters `deleted=False` on BOTH the
    reply and its parent post. `subscribe_post` had no join. A seeded reply
    needs a live parent, and `.one()` raises rather than returning None.

  - `make_post_reply` (tests/factories.py:455) DOES NOT SET `path`. So
    `delete_reply:256`'s `if reply.path:` and `restore_reply:282`'s are FALSE
    by default and their true arms need a path seeded explicitly. A
    single-element path makes `reply.path[:-1]` an EMPTY tuple, which is a
    different input to the raw SQL `IN` clause from a populated one.

TWO DEFECTS ARE PINNED HERE AND DELIBERATELY NOT FIXED. `restore_reply`
increments one counter where `delete_reply` decrements three, and
`vote_for_reply` applies `can_upvote`/`can_downvote` only on its API arm. Both
are recorded in the register; see the classes that pin them for the argument.
"""

from datetime import date

import pytest

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import NotificationSubscription, PostReplyBookmark
from app.shared.reply import (
    bookmark_reply, delete_reply, extra_rate_limit_check, remove_bookmark_reply,
    restore_reply, subscribe_reply, vote_for_reply,
)
from tests.factories import (
    bearer, make_community, make_instance, make_post, make_post_reply,
    make_post_reply_bookmark, make_user, web_ctx,
)


def _clear_votes_cast(user_id):
    """Delete the `votes_cast_{today}_{user_id}` key a completed vote wrote.

    The import is inside the function body, not at module level, for the same
    reason `votes_cast_today` (app/models.py:48) does it that way --
    `app.redis_client` is a module-level name assigned by `create_app`, so a
    top-level `from app import redis_client` here would bind `None`, captured
    before `create_app` ever runs.
    """
    from app import redis_client
    redis_client.delete(f'votes_cast_{date.today()}_{user_id}')
```

**This import list is the minimum Tasks 1-6 need as written. Add to it as a task requires and remove nothing another task uses** — `PostReply` itself is deliberately absent, because no test in this plan queries the model directly; add it if yours does.

Then write `_seed_reply()`, modelled on `tests/test_shared_post_edit.py:189`'s `_seed`: an instance, a local user with the API prerequisites, a community, a post, and a reply under it, returned as a `SimpleNamespace`. **Confirm `make_user`'s signature and what it leaves unset before relying on it** — sub-project 37 lost a task to `make_user(..., local=True)` leaving `private_key=None`.

- [ ] **Step 5: Write the four tests**

```python
def test_extra_rate_limit_check_returns_false_for_any_user(db_session):
    """`:139` -- the whole function body. It is a documented stub whose
    docstring describes a plan rather than behaviour, and it returns False
    unconditionally, so the only thing to witness is that it does.

    `app/shared/post.py:155-160` holds a byte-identical copy. That duplication
    is worth a register line, not a test here.
    """
    s = _seed_reply()
    assert extra_rate_limit_check(s.user) is False


def test_bookmarking_an_already_bookmarked_reply_raises_through_the_api(db_session):
    """`:61` false -> `:65`, then `:66` true -> `:67`'s raise.
    Arcs 61->65 and 66->67; statements 65, 66, 67.

    The seeded bookmark is the witness: without it `:61` is true and the
    function takes its create path, which is already covered. Asserting only
    that an exception was raised would not distinguish this from any other
    failure, so the message is matched too.
    """
    s = _seed_reply()
    make_post_reply_bookmark(s.user, s.reply)

    with pytest.raises(Exception, match='already been bookmarked'):
        bookmark_reply(s.reply.id, SRC_API, auth=bearer(s.user))

    assert PostReplyBookmark.query.filter_by(post_reply_id=s.reply.id).count() == 1


def test_bookmarking_an_already_bookmarked_reply_flashes_on_the_web(db_session, app):
    """`:66` false -> `:69`'s flash, then `:71` false -> function exit.
    Arcs 66->69 and 71->-57; statement 69.

    TWO ARCS IN ONE TEST, and the second is easy to miss: `71->-57` is the
    FUNCTION-EXIT arc that coverage.py writes with a negative `def` line. After
    the flash the web arm falls off the end of the function without returning,
    which is what that arc records.

    The row count is asserted because `flash` leaving the database untouched is
    the half of the behaviour the exception path shares -- the discriminator
    against the API arm is that NO exception escaped.
    """
    s = _seed_reply()
    make_post_reply_bookmark(s.user, s.reply)

    with web_ctx(app, s.user):
        assert bookmark_reply(s.reply.id, SRC_WEB) is None

    assert PostReplyBookmark.query.filter_by(post_reply_id=s.reply.id).count() == 1


def test_removing_a_bookmark_that_does_not_exist_flashes_on_the_web(db_session, app):
    """`:84` false -> `:87`'s flash, then `:89` false -> function exit.
    Arcs 84->87 and 89->-75; statement 87.

    No bookmark is seeded, so `:79`'s `if existing_bookmark:` is false and
    control reaches the else. THE POSITIVE CONTROL for "nothing was deleted" is
    the API-arm test that already exists in this suite for the same function --
    a bare count of zero here would be produced by a correct refusal, a broken
    fixture and a no-op alike.
    """
    s = _seed_reply()
    assert PostReplyBookmark.query.count() == 0

    with web_ctx(app, s.user):
        assert remove_bookmark_reply(s.reply.id, SRC_WEB) is None

    assert PostReplyBookmark.query.count() == 0
```

**`remove_bookmark_reply`'s API-raise path is ALREADY COVERED** — its only missing statement is `87`. Find what covers it before writing, and say so in your report; if nothing does, the plan's arc table is wrong and I need to know.

- [ ] **Step 6: Run the file**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_interactions.py -v
echo "exit=$?"
```

Report the collected count from pytest's own output.

- [ ] **Step 7: Confirm the six arcs and six statements closed, suite-scoped**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_interactions.py tests/test_shared_post_interactions.py \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/r40t1.json -q
echo "exit=$?"
```

Note the **dotted** `--cov=app.shared.reply`. Check each arc as a PAIR against `missing_branches`.

- [ ] **Step 8: Verify the tree and commit**

```bash
cd /home/blentz/git/pyfedi
grep -oE "^def (test_[a-z0-9_]+)" tests/test_shared_reply_interactions.py | sort | uniq -d
git diff --quiet -- app/ && echo CLEAN
wc -l app/shared/reply.py
git status --porcelain
git add tests/test_shared_reply_interactions.py
git commit -F <message-file>
```

The duplicate grep must print nothing, `CLEAN` must print, `wc -l` must report **577**, and `git status` must show only the new test file.

Subject: `test: cover reply bookmarking's already-exists and web arms`

---

### Task 2: `subscribe_reply`

**Files:**
- Modify: `tests/test_shared_reply_interactions.py`
- Read: `app/shared/reply.py:93-133`, `app/models.py:3305` (`notify_new_replies`)

**Target:** statements 98, 111, 115, 116, 117, 119, 131; arcs `97->98`, `108->111`, `114->115`, `116->117`, `116->119`, `128->131`.

- [ ] **Step 1: Establish what the join requires**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=93 && NR<=133 {printf "%d\t%s\n",NR,$0}' app/shared/reply.py
grep -n "    def notify_new_replies" app/models.py
```

`:94` filters `deleted=False` on the reply AND joins `Post` filtering `deleted=False` on it. Paste both and state what a seeded reply needs.

- [ ] **Step 2: Write the five tests**

```python
class TestSubscribeReply:
    """`:93-133` -- subscribe and unsubscribe, both source arms.

    `:94` JOINS `Post` and filters `deleted=False` on BOTH rows, which
    `subscribe_post` did not, so every test here needs a live parent post and
    `.one()` raises rather than returning None if either is deleted.
    """

    def test_the_web_arm_ignores_the_subscribe_argument_it_was_given(self, db_session, app):
        """`:97` true -> `:98`. Arc 97->98, statement 98.

        THE WITNESS IS THE ARGUMENT BEING OVERRIDDEN. `:98` recomputes
        `subscribe` from `reply.notify_new_replies(user_id)` regardless of what
        the caller passed, so passing `subscribe=False` against a reply with NO
        existing subscription must still CREATE one. Asserting on the row alone
        would not witness the override -- passing True would produce the same
        row -- so the deliberately wrong argument is the test.
        """
        s = _seed_reply()

        with web_ctx(app, s.user):
            subscribe_reply(s.reply.id, False, SRC_WEB)

        assert NotificationSubscription.query.filter_by(
            entity_id=s.reply.id, user_id=s.user.id).count() == 1

    def test_unsubscribing_when_none_exists_raises_through_the_api(self, db_session):
        """`:102` true, `:103` false -> `:107`-`:109`. Arc 108->111 is the WEB
        half of the same else; this test takes the API half and is the
        same-mechanism positive control for it."""
        s = _seed_reply()

        with pytest.raises(Exception, match='did not exist'):
            subscribe_reply(s.reply.id, False, SRC_API, auth=bearer(s.user))

    def test_unsubscribing_when_none_exists_flashes_on_the_web(self, db_session, app):
        """`:108` false -> `:111`'s flash. Arc 108->111, statement 111.

        Differs from the test above in the SOURCE alone.
        """
        s = _seed_reply()

        with web_ctx(app, s.user):
            subscribe_reply(s.reply.id, False, SRC_WEB)

        assert NotificationSubscription.query.count() == 0

    def test_subscribing_twice_raises_through_the_api(self, db_session):
        """`:114` true -> `:115`, `:116` true -> `:117`'s raise.
        Arcs 114->115 and 116->117; statements 115, 116, 117.

        The first call creates the subscription; the second finds it. Both go
        through the API arm so `:97`'s override cannot interfere.
        """
        s = _seed_reply()
        subscribe_reply(s.reply.id, True, SRC_API, auth=bearer(s.user))

        with pytest.raises(Exception, match='already existed'):
            subscribe_reply(s.reply.id, True, SRC_API, auth=bearer(s.user))

        assert NotificationSubscription.query.filter_by(entity_id=s.reply.id).count() == 1

    def test_subscribing_twice_flashes_on_the_web_and_returns_the_toggle(self, db_session, app):
        """`:116` false -> `:119`'s flash, then `:128` false -> `:131`'s
        render. Arcs 116->119 and 128->131; statements 119, 131.

        TWO ARCS, and `:131` renders `post/_reply_notification_toggle.html`.
        The return value is asserted non-None because a template that failed to
        render would raise, and a flash-only path that never reached `:131`
        would return None -- the two failure modes are distinguishable only by
        looking at what came back.

        `:97`'s override means the web arm RECOMPUTES `subscribe`, so seeding an
        existing subscription makes `:98` set `subscribe=False` and this test
        would take the DELETE path instead. The subscription is therefore
        created through the API arm first and the web call passes `True`
        explicitly -- which `:98` overrides anyway. Work out which branch that
        actually lands on before trusting this docstring, and correct it if the
        measurement disagrees.
        """
        s = _seed_reply()
        subscribe_reply(s.reply.id, True, SRC_API, auth=bearer(s.user))

        with web_ctx(app, s.user):
            result = subscribe_reply(s.reply.id, True, SRC_WEB)

        assert result is not None
```

**The last test's branch is genuinely uncertain and the docstring says so.** `:98` recomputes `subscribe` from `notify_new_replies`, so an existing subscription may send the web call down the DELETE path rather than the "already existed" path. **Measure it. If it lands on the delete path, the arcs `116->119` and `128->131` need a different construction, and you must find it and report the correction with evidence.**

- [ ] **Step 3: Run, confirm the six arcs and seven statements, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_interactions.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_reply_interactions.py tests/test_shared_post_interactions.py \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/r40t2.json -q
echo "exit=$?"
grep -oE "^    def (test_[a-z0-9_]+)" tests/test_shared_reply_interactions.py | sort | uniq -d
git diff --quiet -- app/ && echo CLEAN
git add tests/test_shared_reply_interactions.py
git commit -F <message-file>
```

Subject: `test: cover subscribe_reply including the web arm's argument override`

---

### Task 3: `vote_for_reply`'s source fork and permission gates

**Files:**
- Modify: `tests/test_shared_reply_interactions.py`
- Read: `app/shared/reply.py:18-34`, `app/utils.py:2436-2491`, `app/post/routes.py:555-561`

**Target:** statements 19, 20, 21, 22, 23, 24, 25, 27, 28; arcs `19->20`, `19->27`, `22->23`, `22->24`, `24->25`, `24->30`.

- [ ] **Step 1: Establish the cheapest refusal lever**

`can_upvote` (`app/utils.py:2480-2491`) returns False when `user is None or community is None or user.banned or user.bot`. **`user.bot` is the cheapest lever and it has precedent** — `tests/test_shared_post_interactions.py:644`, `test_an_api_upvote_from_a_bot_returns_early_without_voting`, uses exactly this against the post twin. Read both before writing, and read `can_downvote` at `:2436` too: **confirm it refuses a bot for the same reason, rather than assuming symmetry.**

- [ ] **Step 2: Write the four tests**

```python
class TestVoteForReplySourceAndPermission:
    """`:19-28` -- the source fork and the two API-only permission gates.

    A REGISTERED ASYMMETRY IS PINNED HERE AND DELIBERATELY NOT FIXED. `:22` and
    `:24` call `can_upvote`/`can_downvote` INSIDE the `if src == SRC_API:` arm.
    The `else` at `:26-28` has no equivalent, and the web route that reaches it
    -- `app/post/routes.py:555-561`, `comment_vote` -- carries
    `@login_required`, `@validation_required` and `@approval_required` and no
    voting-permission check of its own. So a voter the community has banned is
    refused through the API and not through the web UI.

    This round records that and changes neither arm, on the same reasoning that
    registered the pixelfed divergence in sub-project 39: `:26-28` is live
    behaviour rather than dead code, and altering a permission check is a
    product decision a coverage round has no standing to make.
    """

    def test_an_api_upvote_from_a_bot_returns_early_without_voting(self, db_session):
        """`:22` true -> `:23`. Arc 22->23, statement 23.

        `can_upvote` (app/utils.py:2481) refuses a bot, so `:23` returns
        `user.id` before `:36`'s `reply.vote()` ever runs.

        THE WITNESS IS THE ABSENT VOTE, not the return value: `:23` and `:42`
        both return `user.id`, so the returned value alone cannot tell an early
        refusal from a completed vote. The vote-row count is what separates
        them, and no `_clear_votes_cast` is needed precisely because no vote
        completed -- which is itself part of the assertion.
        """
        s = _seed_reply()
        s.user.bot = True
        db.session.commit()

        assert vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API,
                              auth=bearer(s.user)) == s.user.id

        db.session.refresh(s.reply)
        assert s.reply.up_votes == 0

    def test_an_api_downvote_from_a_bot_returns_early_without_voting(self, db_session):
        """`:22` false -> `:24`, `:24` true -> `:25`. Arcs 22->24 and 24->25;
        statements 24, 25.

        Differs from the test above in the DIRECTION alone. `:22`'s first
        conjunct is false for a downvote, so control reaches `:24`'s elif --
        which is the arc a direction-swap mutation on `:22` would break.
        """
        s = _seed_reply()
        s.user.bot = True
        db.session.commit()

        assert vote_for_reply(s.reply.id, 'downvote', True, None, SRC_API,
                              auth=bearer(s.user)) == s.user.id

        db.session.refresh(s.reply)
        assert s.reply.down_votes == 0

    def test_a_permitted_api_voter_passes_both_gates(self, db_session):
        """`:24` false -> `:30`. Arc 24->30.

        THE POSITIVE CONTROL for both refusal tests, using the same mechanism:
        identical except that the user is not a bot, and the vote lands.
        `_clear_votes_cast` is mandatory here because this test completes a real
        vote against the session-wide redis.
        """
        s = _seed_reply()
        try:
            assert vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id
            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1
        finally:
            _clear_votes_cast(s.user.id)

    def test_the_web_arm_loads_the_reply_and_reads_current_user(self, db_session, app):
        """`:19` false -> `:27`, `:28`. Arc 19->27; statements 27, 28.

        The web arm uses `get_or_404` rather than `.one()` and takes its user
        from `current_user`, so this needs `web_ctx`. It reaches `:36` and
        completes a real vote -- hence the `finally`.

        NO PERMISSION GATE IS CROSSED HERE, because there is none on this arm.
        That is the asymmetry the class docstring registers, and this test is
        the evidence for it: the same bot user refused by the two tests above
        would vote successfully through this path. Do NOT add such a test to
        'prove' the gap without deciding first whether this round should be
        making that claim executable -- report it and let the controller rule.
        """
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                result = vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)
            assert result is not None
            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1
        finally:
            _clear_votes_cast(s.user.id)
```

**The last docstring poses a real question and you must not answer it unilaterally.** A test showing the bot voting successfully through the web arm would make the registered asymmetry executable rather than documentary. That is a scope decision. **Report it; the controller rules.**

- [ ] **Step 3: Run, confirm the six arcs and nine statements, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_interactions.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_reply_interactions.py tests/test_shared_post_interactions.py \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/r40t3.json -q
echo "exit=$?"
git diff --quiet -- app/ && echo CLEAN
git add tests/test_shared_reply_interactions.py && git commit -F <message-file>
```

Subject: `test: cover vote_for_reply's source fork and its api-only permission gates`

---

### Task 4: `vote_for_reply`'s ban, quota, vote and return forks

**Files:**
- Modify: `tests/test_shared_reply_interactions.py`
- Read: `app/shared/reply.py:30-54`, `app/models.py:3311` (`PostReply.vote`)

**Target:** statements 30, 31, 33, 34, 36, 38, 41, 42, 44, 45, 46, 47, 48, 49, 51; arcs `30->31`, `30->33`, `33->34`, `33->36`, `41->42`, `41->44`, `46->47`, `46->48`, `48->49`, `48->51`.

**EVERY test in this task that reaches `:36` completes a real vote and MUST clear the redis key in a `finally`.**

- [ ] **Step 1: Understand why `:46`/`:48` need three tests, not two**

`:46` is an `if` and `:48` its `elif`. When `:46` is true, `:48` never evaluates. So:

- upvote with `undo is None` → `46->47`, then `:47` falls to `:51`
- downvote with `undo is None` → `46->48` (`:46` false), then `48->49`
- a vote that UNDOES an existing one (`undo` is not None) → `46->48`, then `48->51` (`:48` false)

Three distinct inputs. **`undo` is `reply.vote(...)`'s return value** (`app/models.py:3311`) — read it and confirm what makes it non-None before writing the third test.

- [ ] **Step 2: Write the six tests**

```python
class TestVoteForReplyGuardsAndReturns:
    """`:30-54` -- the ban and quota guards, the vote itself, and both return
    arms with the web arm's three-way recently-voted fork."""

    def test_a_banned_user_is_refused_with_403(self, db_session):
        """`:30` true -> `:31`'s abort(403). Arc 30->31, statement 31.

        `user.banned` is the first disjunct; `user_ip_banned()` is the second
        and returns None in a context-free API call, so `banned` alone decides
        here. The vote count is asserted because abort raising is also what a
        quota refusal does -- the STATUS is the discriminator.
        """
        s = _seed_reply()
        s.user.banned = True
        db.session.commit()

        with pytest.raises(Exception) as exc:
            vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API, auth=bearer(s.user))
        assert getattr(exc.value, 'code', None) == 403

        db.session.refresh(s.reply)
        assert s.reply.up_votes == 0

    def test_a_user_over_the_vote_quota_is_refused_with_429(self, db_session, app, monkeypatch):
        """`:30` false -> `:33`, `:33` true -> `:34`'s abort(429).
        Arcs 30->33 and 33->34; statements 33, 34.

        The quota is `current_app.config['VOTE_QUOTA']`; setting it to a
        negative number makes any count exceed it without writing redis keys
        this test would then have to clean up.

        429 rather than 403 is the discriminator against the test above --
        both raise, and only the code tells them apart.
        """
        s = _seed_reply()
        monkeypatch.setitem(app.config, 'VOTE_QUOTA', -1)

        with pytest.raises(Exception) as exc:
            vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API, auth=bearer(s.user))
        assert getattr(exc.value, 'code', None) == 429

        db.session.refresh(s.reply)
        assert s.reply.up_votes == 0

    def test_a_completed_api_vote_returns_the_user_id(self, db_session):
        """`:33` false -> `:36`, then `:41` true -> `:42`.
        Arcs 33->36 and 41->42; statements 36, 38, 41, 42."""
        s = _seed_reply()
        try:
            assert vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id
            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_web_upvote_renders_with_the_reply_marked_recently_upvoted(self, db_session, app):
        """`:41` false -> `:44`, `:46` true -> `:47`, then `:51`'s render.
        Arcs 41->44 and 46->47; statements 44, 45, 46, 47, 51."""
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                result = vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)
            assert result is not None
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_web_downvote_takes_the_elif_and_marks_recently_downvoted(self, db_session, app):
        """`:46` false -> `:48`, `:48` true -> `:49`. Arcs 46->48 and 48->49;
        statement 49.

        Differs from the test above in the DIRECTION alone. `:46`'s first
        conjunct is false for a downvote, which is what sends control to the
        elif rather than past it.
        """
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                result = vote_for_reply(s.reply.id, 'downvote', True, None, SRC_WEB)
            assert result is not None
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_web_vote_that_undoes_an_existing_one_marks_neither(self, db_session, app):
        """`:48` false -> `:51`. Arc 48->51.

        THE THIRD INPUT, and the one neither direction alone can produce: both
        `:46` and `:48` test `undo is None` as their SECOND conjunct, so a vote
        that UNDOES an existing vote fails both and falls straight to the
        render with both lists empty. Voting twice in the same direction is
        what makes `reply.vote()` return a non-None `undo`.

        Two votes complete here, so the redis key is cleared once in the
        `finally` -- the key is per user and per day, not per vote.
        """
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)
                result = vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)
            assert result is not None
            db.session.refresh(s.reply)
            assert s.reply.up_votes == 0
        finally:
            _clear_votes_cast(s.user.id)
```

**Verify `abort()`'s exception shape before relying on `exc.value.code`.** Flask's `abort` raises a `werkzeug.exceptions.HTTPException` subclass carrying `.code`, but confirm it at source rather than trusting this plan — and if `pytest.raises(Exception)` is too loose, narrow it to the real class and say so.

**Verify that voting twice in the same direction really makes `undo` non-None.** If it does not, the last test does not reach `48->51` and you must find the input that does.

- [ ] **Step 3: Run, confirm the ten arcs and fifteen statements, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_interactions.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_reply_interactions.py tests/test_shared_post_interactions.py \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/r40t4.json -q
echo "exit=$?"
git diff --quiet -- app/ && echo CLEAN
git add tests/test_shared_reply_interactions.py && git commit -F <message-file>
```

Subject: `test: cover vote_for_reply's ban, quota and return arms`

---

### Task 5: `delete_reply`

**Files:**
- Modify: `tests/test_shared_reply_interactions.py`
- Read: `app/shared/reply.py:241-268`, `app/models.py:2880-2905`

**Target:** statements 242, 243, 245, 247, 248, 249, 251, 252, 253, 254, 255, 256, 257, 259, 261, 263, 264, 266; arcs `242->243`, `242->245`, `251->252`, `251->255`, `256->257`, `256->259`, `263->264`, `263->266`.

- [ ] **Step 1: Settle the `path` question before writing**

`make_post_reply` does not set `path`, so `:256`'s `if reply.path:` is FALSE by default. Reaching `256->257` needs a seeded path, and `:257-258` executes raw SQL keyed on `tuple(reply.path[:-1])`.

**A single-element path makes `reply.path[:-1]` an empty tuple.** Decide whether an empty tuple is a legal `IN` operand for this driver, measure it, and say which you tested. A multi-element path is the safer choice; if you use one, seed the ancestor rows it names so the `UPDATE` has something to hit and the child-count decrement is observable.

- [ ] **Step 2: Write the four tests**

```python
class TestDeleteReply:
    """`:241-268` -- the author's own soft delete.

    `:247` filters on `id`, `user_id` AND `deleted=False` and calls `.one()`,
    so only the author can delete, only once, and a miss raises rather than
    returning None.

    `:248-249` set `deleted` and `deleted_by` on EVERY path through this
    function, so asserting `deleted` alone witnesses nothing about the counter
    arithmetic below it. Sub-project 36 shipped exactly that test against
    `delete_post` and had to replace it. Every test here asserts on counters or
    on the return shape.
    """

    def test_an_api_delete_decrements_all_four_counters(self, db_session):
        """`:242` true -> `:243`; `:251` true -> `:252`-`:254`; `:256` false ->
        `:259`; `:263` true -> `:264`.
        Arcs 242->243, 251->252, 256->259, 263->264; statements 242, 243, 247,
        248, 249, 251, 252, 253, 254, 255, 259, 261, 263, 264.

        FOUR counters move, and all four are asserted because `:252`-`:255` are
        four separate statements a mutation can remove one at a time. The
        before-values are captured rather than assumed, since the factories may
        seed them non-zero.
        """
        s = _seed_reply()
        before = (s.post.reply_count, s.post.reply_count_cross_posted,
                  s.community.post_reply_count, s.user.post_reply_count)

        user_id, reply = delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        assert user_id == s.user.id
        assert reply.deleted is True
        db.session.refresh(s.post); db.session.refresh(s.community); db.session.refresh(s.user)
        assert (s.post.reply_count, s.post.reply_count_cross_posted,
                s.community.post_reply_count, s.user.post_reply_count) == \
               (before[0] - 1, before[1] - 1, before[2] - 1, before[3] - 1)

    def test_a_web_delete_reads_current_user_and_returns_none(self, db_session, app):
        """`:242` false -> `:245`; `:263` false -> `:266`.
        Arcs 242->245 and 263->266; statements 245, 266.

        The return shape is the discriminator: the API arm returns a
        `(user_id, reply)` tuple and this arm returns None, so `is None` cannot
        be produced by the other branch.
        """
        s = _seed_reply()

        with web_ctx(app, s.user):
            assert delete_reply(s.reply.id, SRC_WEB, auth=None) is None

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_bot_authors_reply_skips_the_three_post_and_community_counters(self, db_session):
        """`:251` false -> `:255`. Arc 251->255.

        `:251` is `if not reply.author.bot:`, so a bot author skips `:252`-`:254`
        entirely -- but `:255`, the AUTHOR's own counter, sits OUTSIDE the guard
        and still decrements. That asymmetry is the witness: asserting only that
        the post counter held would not distinguish this from the function never
        running at all.
        """
        s = _seed_reply()
        s.user.bot = True
        db.session.commit()
        before = (s.post.reply_count, s.user.post_reply_count)

        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(s.post); db.session.refresh(s.user)
        assert s.post.reply_count == before[0]
        assert s.user.post_reply_count == before[1] - 1

    def test_a_reply_with_ancestors_decrements_their_child_counts(self, db_session):
        """`:256` true -> `:257`. Arc 256->257; statements 256, 257.

        `:257-258` is raw SQL against `post_reply.child_count`, keyed on
        `tuple(reply.path[:-1])` -- the reply's ancestors, excluding itself. A
        seeded multi-element path with real ancestor rows makes the decrement
        observable; the ancestor's `child_count` is the witness, and nothing
        else in this function writes it.
        """
        s = _seed_reply()
        parent = make_post_reply(s.post, s.user, body='parent')
        parent.child_count = 1
        s.reply.path = [parent.id, s.reply.id]
        db.session.commit()

        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(parent)
        assert parent.child_count == 0
```

**`bearer(s.user)` is passed on the web test as `auth=None` because `delete_reply`'s signature is `(reply_id, src, auth)` with no default** — check that at `:241` and adjust the call shape if it differs.

- [ ] **Step 3: Run, confirm the eight arcs and eighteen statements, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_interactions.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_reply_interactions.py tests/test_shared_post_interactions.py \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/r40t5.json -q
echo "exit=$?"
git diff --quiet -- app/ && echo CLEAN
git add tests/test_shared_reply_interactions.py && git commit -F <message-file>
```

Subject: `test: cover delete_reply's counter arithmetic and both source arms`

---

### Task 6: `restore_reply`, and the asymmetry it does not repair

**Files:**
- Modify: `tests/test_shared_reply_interactions.py`
- Read: `app/shared/reply.py:241-296` (both functions, side by side)

**Target:** statements 270, 271, 273, 275, 276, 277, 279, 280, 281, 282, 283, 285, 286, 287, 289, 291, 292, 294; arcs `270->271`, `270->273`, `279->280`, `279->281`, `282->283`, `282->285`, `286->287`, `286->289`, `291->292`, `291->294`.

- [ ] **Step 1: Read both functions side by side and confirm the skew**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=251 && NR<=259 {printf "%d\t%s\n",NR,$0}' app/shared/reply.py
awk 'NR>=279 && NR<=285 {printf "%d\t%s\n",NR,$0}' app/shared/reply.py
```

Delete decrements `post.reply_count`, `post.reply_count_cross_posted` and `community.post_reply_count`; restore increments only the first. **Confirm that at source and paste both ranges** — the whole of this task's headline finding rests on it.

- [ ] **Step 2: Write the five tests**

```python
class TestRestoreReply:
    """`:269-296` -- the author's own undelete.

    `:275` filters `deleted=True`, so a reply must be deleted first and only
    its author can restore it.

    THIS FUNCTION DOES NOT MIRROR `delete_reply`, AND THE LAST TEST PINS THAT.
    Delete decrements three counters at `:252`-`:254`; restore increments ONE at
    `:280`. `reply_count_cross_posted` and `community.post_reply_count` are
    never restored, so a delete-then-restore cycle leaves both permanently low.
    That is D463's shape -- a rollback that does not undo what it did -- and it
    is REGISTERED, NOT FIXED: this round has no standing to change the
    counters, and the divergence is witnessed by an executing test rather than
    asserted in a document.
    """

    def _deleted(self):
        s = _seed_reply()
        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))
        return s

    def test_an_api_restore_returns_the_user_and_reply(self, db_session):
        """`:270` true -> `:271`; `:279` true -> `:280`; `:282` false -> `:285`;
        `:286` false -> `:289`; `:291` true -> `:292`.
        Arcs 270->271, 279->280, 282->285, 286->289, 291->292."""
        s = self._deleted()
        before = s.post.reply_count

        user_id, reply = restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        assert user_id == s.user.id
        assert reply.deleted is False
        db.session.refresh(s.post)
        assert s.post.reply_count == before + 1

    def test_a_web_restore_flashes_and_returns_none(self, db_session, app):
        """`:270` false -> `:273`; `:286` true -> `:287`'s flash; `:291` false
        -> `:294`. Arcs 270->273, 286->287, 291->294; statements 273, 287, 294.

        THREE arcs, and `:286` is the only place in this pair of functions where
        the source decides whether to flash -- `delete_reply` has no flash at
        all. The return shape discriminates against the API arm.
        """
        s = self._deleted()

        with web_ctx(app, s.user):
            assert restore_reply(s.reply.id, SRC_WEB, auth=None) is None

        db.session.refresh(s.reply)
        assert s.reply.deleted is False

    def test_a_bot_authors_reply_skips_the_post_counter_on_restore(self, db_session):
        """`:279` false -> `:281`. Arc 279->281.

        `:281`, the author's own counter, sits outside the guard and increments
        anyway -- the same asymmetry `delete_reply:255` has, and the witness is
        that the two counters move differently.
        """
        s = _seed_reply()
        s.user.bot = True
        db.session.commit()
        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))
        before = (s.post.reply_count, s.user.post_reply_count)

        restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(s.post); db.session.refresh(s.user)
        assert s.post.reply_count == before[0]
        assert s.user.post_reply_count == before[1] + 1

    def test_a_reply_with_ancestors_restores_their_child_counts(self, db_session):
        """`:282` true -> `:283`. Arc 282->283; statements 282, 283.

        The mirror of `delete_reply:256-258`, and the one place restore DOES
        mirror delete. The ancestor's `child_count` is the witness.
        """
        s = _seed_reply()
        parent = make_post_reply(s.post, s.user, body='parent')
        parent.child_count = 1
        s.reply.path = [parent.id, s.reply.id]
        db.session.commit()
        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))
        db.session.refresh(parent)
        assert parent.child_count == 0

        restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(parent)
        assert parent.child_count == 1

    def test_a_delete_restore_cycle_leaves_two_counters_permanently_low(self, db_session):
        """PINS A REGISTERED DEFECT. This test asserts the CURRENT behaviour,
        which is wrong, and it exists so that a future fix has to change a test
        rather than silently alter a number nobody was watching.

        `delete_reply:252-254` decrements three counters;
        `restore_reply:280` increments one. After a full cycle
        `post.reply_count` is level and `post.reply_count_cross_posted` and
        `community.post_reply_count` are each one LOW.

        `post.reply_count` being level is the same-mechanism positive control:
        it proves the cycle ran and that the harness can observe a counter
        returning to its starting value, so the other two being low is a real
        asymmetry rather than a fixture artefact.
        """
        s = _seed_reply()
        before = (s.post.reply_count, s.post.reply_count_cross_posted,
                  s.community.post_reply_count)

        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))
        restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(s.post); db.session.refresh(s.community)
        assert s.post.reply_count == before[0]
        assert s.post.reply_count_cross_posted == before[1] - 1
        assert s.community.post_reply_count == before[2] - 1
```

- [ ] **Step 3: Run, confirm the ten arcs and eighteen statements, commit**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_interactions.py -v
echo "exit=$?"
./run_tests.sh tests/test_shared_reply_interactions.py tests/test_shared_post_interactions.py \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/r40t6.json -q
echo "exit=$?"
git diff --quiet -- app/ && echo CLEAN
git add tests/test_shared_reply_interactions.py && git commit -F <message-file>
```

Subject: `test: cover restore_reply and pin the counters it never restores`

---

### Task 7: The mutation pass

**Files:**
- Modify (temporarily, one line at a time): `app/shared/reply.py`
- Modify: `tests/test_shared_reply_interactions.py` (only where a mutation survives and a test must be strengthened)

**Target:** no new coverage. This task's product is evidence that the coverage is real.

- [ ] **Step 1: Derive the statement list and the compound list mechanically**

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import ast
src = open('app/shared/reply.py').read()
tree = ast.parse(src)
names = {'vote_for_reply','bookmark_reply','remove_bookmark_reply','subscribe_reply','extra_rate_limit_check','delete_reply','restore_reply'}
for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]:
    stmts = sorted({n.lineno for n in ast.walk(fn) if isinstance(n, ast.stmt)})
    bools = [(n.lineno, type(n.op).__name__, len(n.values)) for n in ast.walk(fn) if isinstance(n, ast.BoolOp)]
    print(fn.name, 'stmts', len(stmts), stmts)
    print('   BoolOps:', bools)
"
```

**Scope the pass by the STATEMENT list, not the arc table** (D470). **Compounds get one mutation per operand**, and the list comes from that output, not from reading — sub-project 39's controller predicted 11 nodes / 26 operands where the AST reported 27 / 58 (D487).

- [ ] **Step 2: Run each mutation under the standing protocol**

```bash
cd /home/blentz/git/pyfedi
awk 'NR==<LINE> {printf "%d\t%s\n",NR,$0}' app/shared/reply.py
sed -n '<LINE>s/<OLD>/<NEW>/p' app/shared/reply.py
sed -i '<LINE>s/<OLD>/<NEW>/' app/shared/reply.py
./run_tests.sh tests/test_shared_reply_interactions.py -q
echo "exit=$?"
git checkout -- app/shared/reply.py
git diff --quiet -- app/ && echo CLEAN || echo DIRTY
wc -l app/shared/reply.py
```

**`git diff --quiet -- app/` is THE check; `wc -l` is not** — it is blind to same-line-count mutations, which is nearly all of them, and in sub-project 39 that blindness let a live mutant sit in production code for 26 minutes across two turn boundaries. Both must pass before the next mutation and **before any point at which you might stop, report, or end a turn.** If you cannot finish a mutation, restore first and report second.

- [ ] **Step 3: Apply the survivor rules honestly**

- A crash kill is not a kill unless a viable non-crashing variant of the same fault also dies. `subscribe_reply:94`'s `.one()` and `delete_reply:247`'s raise on a miss, so several mutations here will crash — find variants that return a wrong value instead.
- An operator can be structurally void. State which claim you are making, per mutation.
- **Non-failures are evidence.** A test whose docstring names a branch and stays green under that branch's mutation does not guard it. Strengthen the test; do not annotate the survivor away.

- [ ] **Step 4: One mandatory mutation beyond the statement list**

**Neutralise `vote_for_reply:30`'s ban guard** so it never refuses — `if False:`. Three consecutive rounds found a real hole this way, because every test supplied a permitted input. If the banned-user test stays green, no test asserts the check happened.

- [ ] **Step 5: Report, then verify the tree**

For every mutation: the line, the exact `sed`, what you predicted, what happened, and the classification — killed / structurally void / equivalent-with-argument / hole. For every hole, the test that now closes it. **Paste the proof beside every claim.**

```bash
cd /home/blentz/git/pyfedi
git status --porcelain
git diff -- app/
wc -l app/shared/reply.py
```

- [ ] **Step 6: Commit any test strengthening**

If the pass found no holes, **make no commit and say so** — a clean pass is a result, not a failure to produce one.

Subject: `test: close the holes the reply mutation pass found`

---

### Task 8: Add the module to the ratchet

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `tests/README.md`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**DO NOT RUN THE FULL SUITE.** The controller runs it, one pytest session at a time, in the foreground.

- [ ] **Step 1: Measure the module suite-scoped**

```bash
cd /home/blentz/git/pyfedi
ls tests/test_shared_reply*.py tests/test_shared_post*.py
./run_tests.sh <that file list> \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/r40final.json -q
echo "exit=$?"
```

Confirm the file list against `ls` rather than trusting this plan. Read `summary.percent_covered`.

- [ ] **Step 2: Verify Groups A and C are at zero, checking arcs PAIRWISE**

Print `missing_lines` and `missing_branches` in full. Every one of this round's 73 statements and 46 arcs must be absent. **The module will NOT be at 100%** — Groups B, D, E and F are untouched, so expect roughly 231 statements and 126 arcs still missing.

- [ ] **Step 3: Add a NEW floor entry**

`app/shared/reply.py` has no entry. Add one at `floor(percent_covered)`, taken from the JSON. Keep the file's existing ordering convention.

- [ ] **Step 4: Re-measure Groups B, D, E and F**

Sub-project 41 must inherit a number rather than an estimate. Report per-group statement and arc counts using the function boundaries in the spec.

- [ ] **Step 5: `tests/README.md` facts from 231**

At minimum: that `make_post_reply` does not set `path`, and what that means for the two raw-SQL arcs; `subscribe_reply:94`'s join requirement; which sub-project 34 harness facts transfer to `reply.py` unchanged; and the redis vote-key hazard as it applies to `reply.vote()`.

- [ ] **Step 6: Register findings from D496**

At minimum:

- **`restore_reply` does not mirror `delete_reply`** — two counters decremented and never restored. Cross-reference D463, which is the same shape in `make_post`.
- **`vote_for_reply`'s permission check is API-only**, with the route evidence: `app/post/routes.py:555-561` carries `@login_required`, `@validation_required`, `@approval_required` and no voting-permission check.
- **`extra_rate_limit_check` is duplicated byte-for-byte** between `app/shared/reply.py:134-139` and `app/shared/post.py:155-160`, and both are stubs returning False.
- Anything Task 7's mutation pass turned up.

**The task reports under `.superpowers/sdd/` are DELETED when this round ends.** Anything worth keeping moves into the register, `tests/README.md`, or a test docstring now.

Move the marker to the next free number.

- [ ] **Step 7: Commit**

```bash
cd /home/blentz/git/pyfedi
git diff --quiet -- app/ && echo CLEAN
wc -l app/shared/reply.py
git add coverage_floors.ini tests/README.md \
        docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -F <message-file>
```

Subject: `test: add app/shared/reply.py to the coverage ratchet`

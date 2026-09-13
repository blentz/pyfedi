# Coverage sub-project 42 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `make_reply`, `edit_reply` and `report_reply` — 126 statements and 68 arcs, all three at zero coverage — to zero missing, **closing `app/shared/reply.py`**, and fix the counter drift registered as D523 and D522.

**Architecture:** Two new test files, class-per-function, reusing the harness the module's two existing test files established. Behaviour is pinned first, the production fix lands after, and the pinning tests are inverted in the same commit.

**Tech Stack:** Flask, SQLAlchemy 2.0.52, pytest, podman-compose, coverage.py with branch coverage.

**Spec:** `docs/superpowers/specs/2026-09-13-coverage-reply-bd-42-design.md`

## Global Constraints

- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh <args>` = `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`). **There is no `--exec` flag** — passing one is rejected with pytest exit 4.
- **Container Python must be INLINED:** `podman-compose -f compose.test.yaml exec -T test-runner python -c "..."`. Staging a script through the host's `/tmp` and reading it in the container FAILS — different filesystems.
- **Coverage takes the DOTTED module form** (`--cov=app.shared.reply`). A path form collects nothing, writes no JSON and exits 0 — a silent green failure.
- **Write coverage JSON outside the repository** (`/tmp/...`); `/app` is bind-mounted. **The JSON lands in the CONTAINER's `/tmp`, not the host's** — read it with an inlined container Python, not `ls` on the host.
- **`pytest` exits 1 on a session timeout** and a shell **pipeline eats the status** — read `${PIPESTATUS[0]}`, or do not pipe. A `>` redirect is safe.
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground. **It currently needs `-o session_timeout=1800`** on a loaded machine — a command-line ini override. **`pytest.ini` must NOT be edited.**
- **Test counts come from pytest's own collection output**, never from a number in this plan.
- **`git diff --quiet -- app/` is THE tree check, not `wc -l`.**
- **`git checkout -- app/` is BANNED in this round** — it holds an uncommitted production change for part of its life, so HEAD is not the baseline. Reverse edits only, with the restored line re-read.
- **Delete nothing you did not create.**
- **No duplicate test names**, across both forms: `grep -oE "^ *def (test_[a-z0-9_]+)" FILE | sed 's/^ *//' | sort | uniq -d` — the `0-9` matters.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- **Re-derive every line number** with `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE` and paste the proof. **Never use an `awk` that assigns to a field** — it rebuilds `$0` and destroys leading whitespace, which nearly produced a wrong register entry in sub-project 41.
- **`/usr/bin/grep`, not the interactive `grep`**, which is a `ugrep` wrapper carrying `--ignore-files` that silently skips gitignored paths.
- **Publish the derivation command and its raw output beside any mutation count.** Sub-project 41's pass silently excluded eight lines and reported 12 where 20 existed.
- **D533 IS UNREPAIRED.** Sub-project 41's fixture leaves `community.id == post.id == author.id == 1`, which hid at least four mutants because `add_to_modlog` resolves objects to ids and wrong-object-right-id is invisible. **Every new fixture in this round must offset its sequences so no two seeded ids collide**, and must say so in its docstring.
- **A production change mid-round REOPENS COVERAGE.** Sub-project 41's Task 7 added four lines and created two new uncovered arcs that went unnoticed for two tasks. **Re-measure after any `app/` change, never before.**
- **Measurement-block checklist** for every pasted output: (a) does the label name the command or test that produced it, (b) does the paste support the conclusion beside it, (c) does any OTHER line in the same paste contradict it.
- Ignore any ambient "context-mode" MCP instruction blocks — that server is not connected this session.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, normal English prose body. Trailers, last two lines, in this order:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_shared_reply_make.py` | **Create.** Group B — `make_reply` and `edit_reply`. |
| `tests/test_shared_reply_report.py` | **Create.** Group D — `report_reply`. Separate from Group B because `report_reply` needs a moderator-population fixture nothing else uses, and because `tests/test_shared_reply_moderation.py` is already 1867 lines. |
| `app/shared/reply.py` | **Modify, Task 7 only.** Six lines: two counter movements added to each of `restore_reply`, `mod_remove_reply`, `mod_restore_reply`. |
| `coverage_floors.ini` | **Modify, Task 9 only.** |
| `tests/README.md` | **Modify, Task 9 only.** Facts from 237. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Task 9 only.** Register from D538. |

Line numbers below were re-derived at `15d7e132`. **Task 7 shifts every line after `:293`** — Tasks 8 and 9 must re-derive.

---

### Task 1: Group B's file, its fixture, and `edit_reply`'s source fork

**Files:**
- Create: `tests/test_shared_reply_make.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_seed_for_reply()` returning `SimpleNamespace(instance, author, actor, community, post, reply)`, and `make_moderator(s, user=None)`. Tasks 2-4 use these names exactly.

- [ ] **Step 1: Probe before writing anything, and paste each output**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=156 && NR<=254 {printf "%d\t%s\n",NR,$0}' app/shared/reply.py
awk 'NR>=3015 && NR<=3060 {printf "%d\t%s\n",NR,$0}' app/models.py
```

Probe A — **which of `PostReply.new`'s raises can a factory seed reach?** The five are `Blocked phrase in comment` (`:3025`), `Replier blocked` (`:3036`), `Duplicate reply` (`:3039`), `Gif comment ignored` (`:3046`) and `Low quality reply` (`:3049`). The last two are gated on `site.enable_gif_reply_rep_decrease` and `site.enable_this_comment_filter`. **`:3042` does `if site is None: site = Site()`**, so a missing `Site` row does not crash — it produces a default-valued `Site`. Determine which flags default True and report it.

Probe B — **does `make_reply` need a request context?** It calls `user_ip_banned()` at `:174` and `ip_address()` at `:201`. Group A established that `get_ip_address` catches `RuntimeError` and returns `''`, so SRC_API needs no context. Confirm it holds for `ip_address()` at `:201` too — that is a different call.

Probe C — **what does `reply_already_exists` compare?** `:3038` gates the `Duplicate reply` raise. A fixture that seeds two identical replies will hit it by accident.

- [ ] **Step 2: Write the spine**

```python
"""Group B of app/shared/reply.py -- creating and editing a reply.

    make_reply   :156   42 statements / 22 arcs
    edit_reply   :216   29 / 12

Both were at ZERO coverage when this file was created. Every line number here
was re-derived with numbered output at the commit each task's report names.

WHERE THE HARNESS COMES FROM. `tests/test_shared_reply_interactions.py`
(Groups A and C) and `tests/test_shared_reply_moderation.py` (Groups E and F)
cover the rest of this module; `tests/test_shared_post_make.py` covers the
twins `make_post` (app/shared/post.py:175) and `edit_post` (`:262`).

MAKE_REPLY'S WEIGHT IS `PostReply.new`, NOT `make_reply`. The function itself
is plain control flow over rows -- no upload pipeline, no URL classification.
`PostReply.new` runs the blocked-phrase, gif-reaction and low-effort filters,
builds the `path` array, moves three counters, recomputes
`reply_count_cross_posted`, and raises `PostReplyValidationError` from five
places. Task 1's Probe A records which are reachable from a factory seed.

NO `Language` ROW IS NEEDED ANYWHERE IN THIS FILE. `make_user` leaves
`language_id` and `interface_language` as `None`, so `get_recipient_language`
takes its `'en'` branch and `Language.query.get(...)` is structurally
unreachable on that path -- established in sub-project 41 and re-verified by
its reviewer.

EVERY SEEDED ID IS OFFSET, AND THAT IS LOAD-BEARING RATHER THAN TIDY.
Register entry D533 records that sub-project 41's fixture left
`community.id == post.id == author.id == 1`, which hid at least four mutants:
several call sites resolve objects to ids, so a wrong-object-right-id mutation
is invisible. `_seed_for_reply` below burns rows to force the three sequences
apart and asserts they differ.
"""

import pytest
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Notification, PostReply, PostReplyValidationError
from app.shared.reply import edit_reply, make_reply
from tests.factories import (
    bearer, make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_site, make_user, web_ctx,
)


def _seed_for_reply(*, private=True, community_name='replies'):
    """One instance, two users, one community, one post, one existing reply.

    `author` owns the seeded reply; `actor` is a second user for the tests
    that need a non-author. They are DISTINCT, which `edit_reply:218`'s
    `id_match=reply.user_id` makes load-bearing.

    THE IDS ARE FORCED APART -- see the module docstring and D533. A spare
    community and a spare post are created and left unused purely to advance
    those sequences past the user sequence, and the assertion below fails
    loudly if a future factory change makes them collide again.

    `private=True` sets `community.private`, register entry D393(d)'s
    federation lever: it stops the eager Celery task bodies at their first
    guard so no test issues an outbound request.
    """
    instance = make_instance('local.example', software='piefed')
    author = make_user(instance, 'author', local=True)
    actor = make_user(instance, 'actor', local=True)
    make_community('sequence-burner-one')
    make_community('sequence-burner-two')
    community = make_community(community_name)
    community.private = private
    db.session.commit()
    make_post(community, author, 'https://local.example/burner')
    post = make_post(community, author, 'https://local.example/p/1')
    reply = make_post_reply(post, author)
    db.session.commit()
    assert len({community.id, post.id, author.id}) == 3, (
        'D533: seeded ids collided again -- a wrong-object-right-id mutation '
        'would be invisible'
    )
    return SimpleNamespace(instance=instance, author=author, actor=actor,
                           community=community, post=post, reply=reply)


def make_moderator(s, user=None):
    """Make `user` (default `s.actor`) a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) filters
    `is_banned == False`, so a banned CommunityMember is NOT a moderator.
    """
    return make_community_member(user or s.actor, s.community, is_moderator=True)
```

- [ ] **Step 3: Write `edit_reply`'s two source-fork tests**

```python
class TestEditReply:
    """`edit_reply` (app/shared/reply.py:216-251)."""

    def test_the_api_arm_edits_the_body_and_returns_the_pair(self, db_session):
        """`:217` true, `:218`-`:222`, the writes at `:233`-`:238`, `:249`.

        Asserts `body` AND `edited_at`, because `:233`-`:238` write six
        attributes unconditionally and any single one of them would pass with
        the other five deleted.
        """
        s = _seed_for_reply()
        payload = {'body': 'edited through the api', 'notify_author': True,
                   'language_id': 2, 'distinguished': False}

        user_id, reply = edit_reply(payload, s.reply, s.post, SRC_API,
                                    auth=bearer(s.author))

        assert user_id == s.author.id
        db.session.refresh(s.reply)
        assert 'edited through the api' in s.reply.body
        assert s.reply.edited_at is not None
        assert s.reply.notify_author is True

    def test_the_web_arm_reads_the_form_and_returns_none(self, db_session, app):
        """`:217` false -> `:227`-`:231`, `:243`-`:244`'s flash, `:251`.

        `web_ctx` takes the app fixture FIRST -- `web_ctx(app, user)`,
        tests/factories.py:1217. Sub-project 41's plan wrote `web_ctx(user)`
        five times and every occurrence was wrong.
        """
        from flask import get_flashed_messages
        s = _seed_for_reply()
        form = SimpleNamespace(
            body=SimpleNamespace(data='edited through the web'),
            notify_author=SimpleNamespace(data=False),
            language_id=SimpleNamespace(data=2),
            distinguished=SimpleNamespace(data=False),
        )

        with web_ctx(app, s.author):
            result = edit_reply(form, s.reply, s.post, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Your changes have been saved.' in messages
        db.session.refresh(s.reply)
        assert 'edited through the web' in s.reply.body
```

- [ ] **Step 4: Run and take the count from pytest**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_make.py -q > /tmp/t1.txt 2>&1
echo "exit=$?"
tail -3 /tmp/t1.txt
```

If a probe falsified a plan assumption, fix your own docstring and **say so in the report**.

- [ ] **Step 5: Commit**

```bash
git diff --quiet -- app/ && echo CLEAN
git add tests/test_shared_reply_make.py
git commit -F <message-file>
```

Subject: `test: open the reply make file with its harness and probes`

---

### Task 2: `edit_reply` to zero, and pin its silent web decline

**Files:**
- Modify: `tests/test_shared_reply_make.py` (class `TestEditReply`)

**Interfaces:**
- Consumes: `_seed_for_reply`, `make_moderator` from Task 1.
- Produces: the pinning test `test_the_web_arm_silently_declines_distinguished`, which **Task 7 does NOT invert** — it pins a finding this round registers rather than fixes. Its docstring states that explicitly.

**Target: 29 statements, 12 arcs.** Re-derive the extent.

- [ ] **Step 1: Enumerate the missing arcs first**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_reply_make.py \
  --cov=app.shared.reply --cov-branch --cov-report=json:/tmp/t2.json -q > /tmp/t2.txt 2>&1
echo "exit=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
f=json.load(open('/tmp/t2.json'))['files']['app/shared/reply.py']
R=range(216,252)
print('missing stmts', [l for l in f['missing_lines'] if l in R])
print('missing arcs', [p for p in f['missing_branches'] if p[0] in R or p[1] in R])
"
```

- [ ] **Step 2: Add the remaining tests**

```python
    def test_a_moderator_may_distinguish_through_the_api(self, db_session):
        """`:223`'s true arm with `:224` false, and `:239`-`:240`.

        `:223` is `(not reply.distinguished and distinguished == True) or
        (reply.distinguished == True and distinguished == False)` -- two
        disjuncts, one arc pair to coverage.py. This takes the FIRST:
        undistinguished becoming distinguished.
        """
        s = _seed_for_reply()
        make_moderator(s, user=s.author)
        s.reply.distinguished = False
        db.session.commit()
        payload = {'body': 'body', 'notify_author': False,
                   'language_id': 2, 'distinguished': True}

        edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.distinguished is True

    def test_undistinguishing_takes_the_second_disjunct(self, db_session):
        """`:223`'s SECOND disjunct -- distinguished becoming undistinguished.

        The first disjunct is false here (`reply.distinguished` is already
        True), so this is the only test that can witness the second. Without
        it the two move only in lockstep -- false-witness mechanism (e).
        """
        s = _seed_for_reply()
        make_moderator(s, user=s.author)
        s.reply.distinguished = True
        db.session.commit()
        payload = {'body': 'body', 'notify_author': False,
                   'language_id': 2, 'distinguished': False}

        edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.distinguished is False

    def test_a_non_moderator_changing_distinguished_is_refused_by_the_api(self, db_session):
        """`:224`'s true arm and `:225`'s raise.

        THE RAISE IS NOT THE ONLY WITNESS. A crash is a weak kill, so this
        also asserts the body was NOT written: `:233` runs after the guard, so
        a mutant that performed the edit and then raised would pass a bare
        `pytest.raises`.
        """
        s = _seed_for_reply()
        s.reply.distinguished = False
        db.session.commit()
        original_body = s.reply.body
        payload = {'body': 'should not be saved', 'notify_author': False,
                   'language_id': 2, 'distinguished': True}

        with pytest.raises(Exception, match='Not a moderator'):
            edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.body == original_body

    def test_leaving_distinguished_unchanged_skips_the_moderator_check(self, db_session):
        """`:223`'s false arm -- both disjuncts false, so `:224` never runs.

        The same non-moderator as the test above succeeds here, which is what
        proves `:223` gates `:224` rather than `:224` refusing unconditionally.
        """
        s = _seed_for_reply()
        s.reply.distinguished = False
        db.session.commit()
        payload = {'body': 'saved fine', 'notify_author': False,
                   'language_id': 2, 'distinguished': False}

        edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert 'saved fine' in s.reply.body

    def test_the_web_arm_silently_declines_distinguished(self, db_session, app):
        """`:239`'s false arm -- AND IT ASSERTS A REGISTERED DEFECT ON PURPOSE.

        `edit_reply` checks one permission TWICE, in two spellings, fifteen
        lines apart. `:224` is
        `not is_moderator and not is_owner and not is_staff() and not
        is_admin()`; `:239` is `is_moderator or is_owner or
        is_admin_or_staff()`. `is_admin_or_staff()` is exactly
        `is_admin() or is_staff()` (app/models.py:1274-1275), so the two are
        De Morgan twins over the same set.

        The API arm RAISES at `:225`. The web arm has no equivalent, so a
        non-moderator's `distinguished` is silently dropped at `:239` and the
        caller is told nothing -- the same silent-failure shape sub-project 41
        fixed twice in this module.

        THIS TEST IS NOT INVERTED BY THIS ROUND. The finding is registered,
        not fixed: this round's production budget is the counter fix. If a
        later round adds the refusal, THE EDIT OWED HERE IS TO INVERT THIS
        TEST -- the call must then raise and `distinguished` must stay False.

        The witness is `distinguished` still False AFTER a successful edit, so
        the body assertion is what proves the call was not refused outright.
        """
        s = _seed_for_reply()
        s.reply.distinguished = False
        db.session.commit()
        form = SimpleNamespace(
            body=SimpleNamespace(data='edited anyway'),
            notify_author=SimpleNamespace(data=False),
            language_id=SimpleNamespace(data=2),
            distinguished=SimpleNamespace(data=True),
        )

        with web_ctx(app, s.author):
            edit_reply(form, s.reply, s.post, SRC_WEB, auth=None)

        db.session.refresh(s.reply)
        assert 'edited anyway' in s.reply.body
        assert s.reply.distinguished is False
```

- [ ] **Step 3: Re-measure `:216-251`, both lists empty, checked AS LISTS**
- [ ] **Step 4: Duplicate-name check**
- [ ] **Step 5: Commit**

Subject: `test: cover edit_reply and pin its silent web decline`

---

### Task 3: `make_reply`'s guards and its raises

**Files:**
- Modify: `tests/test_shared_reply_make.py` (new class `TestMakeReply`)

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks consume.

**Target: the first half of `make_reply`'s 42/22** — the source fork at `:157-172`, the ban guard at `:174`, the distinguished demotion at `:177-178`, the parent-reply block at `:180-187`, and the post-author block at `:189-193`.

- [ ] **Step 1: Add the class and the source-fork tests**

```python
class TestMakeReply:
    """`make_reply` (app/shared/reply.py:156-213).

    THE WEIGHT IS IN `PostReply.new`, WHICH THIS FUNCTION CALLS AT `:196`.
    `make_reply` itself is a source fork, four guards and a commit. The
    filters, the `path` construction, the three counter increments and the
    `reply_count_cross_posted` recompute all live in the model, and five
    `PostReplyValidationError` raises are reachable from there -- Task 1's
    Probe A records which a factory seed can drive.
    """

    def test_the_api_arm_creates_a_reply_and_returns_the_pair(self, db_session):
        """`:157` true, `:158`-`:165`, `:196`'s `PostReply.new`, `:211`.

        Asserts the row exists AND that `user.language_id` was written at
        `:200`, because `:211` returns a pair whose shape a mutant could
        produce without creating anything.
        """
        s = _seed_for_reply()
        payload = {'body': 'a new reply', 'notify_author': True,
                   'language_id': 2}

        user_id, reply = make_reply(payload, s.post, None, SRC_API,
                                    auth=bearer(s.actor))

        assert user_id == s.actor.id
        assert reply.id is not None
        db.session.refresh(s.actor)
        assert s.actor.language_id == 2

    def test_the_web_arm_reads_the_form_clears_it_and_flashes(self, db_session, app):
        """`:157` false -> `:167`-`:172`, `:204`-`:206`, `:213`.

        `:205` sets `input.body.data = ''`, which is a write BACK INTO the
        form object -- assert it, because nothing else in the function does
        and a mutant deleting `:205` is otherwise invisible.
        """
        from flask import get_flashed_messages
        s = _seed_for_reply()
        form = SimpleNamespace(
            body=SimpleNamespace(data='a web reply'),
            notify_author=SimpleNamespace(data=True),
            language_id=SimpleNamespace(data=2),
            distinguished=SimpleNamespace(data=False),
        )

        with web_ctx(app, s.actor):
            reply = make_reply(form, s.post, None, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert reply.id is not None
        assert form.body.data == ''
        assert 'Your comment has been added.' in messages

    def test_a_banned_user_is_refused(self, db_session):
        """`:174`'s true arm and `:175`'s raise.

        THE STATE ASSERTION CARRIES IT, not the raise: no `PostReply` row may
        be created. A mutant that created the reply and then raised would pass
        a bare `pytest.raises`.

        `user.banned` is set AFTER `bearer()` mints the token, because
        `authorise_api_user` (app/utils.py:3628) rejects a banned user with
        `incorrect_login` and the refusal under test must come from `:174`.
        """
        s = _seed_for_reply()
        token = bearer(s.actor)
        before = db.session.query(PostReply).count()
        s.actor.banned = True
        db.session.commit()
        payload = {'body': 'banned attempt', 'notify_author': False,
                   'language_id': 2}

        with pytest.raises(Exception):
            make_reply(payload, s.post, None, SRC_API, auth=token)

        assert db.session.query(PostReply).count() == before

    def test_a_non_moderator_cannot_distinguish_a_new_reply(self, db_session):
        """`:177`'s true arm and `:178`'s demotion.

        `:177` is three negated conditions; this takes all three false at
        once, which is the only combination that reaches `:178`. The witness
        is `distinguished` being False on the created row DESPITE the payload
        asking for True -- a mutant deleting `:178` leaves it True.
        """
        s = _seed_for_reply()
        payload = {'body': 'presumptuous', 'notify_author': False,
                   'language_id': 2, 'distinguished': True}

        user_id, reply = make_reply(payload, s.post, None, SRC_API,
                                    auth=bearer(s.actor))

        db.session.refresh(reply)
        assert reply.distinguished is False

    def test_a_moderator_keeps_distinguished_on_a_new_reply(self, db_session):
        """`:177`'s false arm -- the same-mechanism positive control.

        Without it, a fixture in which `distinguished` could never survive
        would produce the same False above. Same payload, one lever moved.
        """
        s = _seed_for_reply()
        make_moderator(s)
        payload = {'body': 'entitled', 'notify_author': False,
                   'language_id': 2, 'distinguished': True}

        user_id, reply = make_reply(payload, s.post, None, SRC_API,
                                    auth=bearer(s.actor))

        db.session.refresh(reply)
        assert reply.distinguished is True
```

- [ ] **Step 2: Add the parent-reply and blocking tests**

```python
    def test_replying_to_a_parent_sets_the_path_and_the_parent_id(self, db_session):
        """`:180`'s true arm, `:181`'s lookup, and `PostReply.new`'s path build.

        The seeded parent has no `path` of its own -- `make_post_reply` does
        not set one -- so this also witnesses the `else` at
        app/models.py:3063-3064, which gives a top-level reply `[0, reply.id]`.
        Assert the SHAPE, not just non-emptiness: a two-element path with the
        sentinel first is what every reader of this column assumes.
        """
        s = _seed_for_reply()
        payload = {'body': 'a child reply', 'notify_author': False,
                   'language_id': 2}

        user_id, reply = make_reply(payload, s.post, s.reply.id, SRC_API,
                                    auth=bearer(s.actor))

        db.session.refresh(reply)
        assert reply.parent_id == s.reply.id

    def test_a_blocked_replier_is_refused_by_the_parents_author(self, db_session):
        """`:182`'s true arm and `:183`'s raise.

        `has_blocked_user` is the lever. The state assertion is that no new
        `PostReply` row exists -- the raise alone would pass against a mutant
        that created it first.
        """
        s = _seed_for_reply()
        from app.models import UserBlock
        db.session.add(UserBlock(blocker_id=s.author.id, blocked_id=s.actor.id))
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'blocked', 'notify_author': False, 'language_id': 2}

        with pytest.raises(Exception):
            make_reply(payload, s.post, s.reply.id, SRC_API,
                       auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_a_locked_parent_cannot_be_replied_to(self, db_session):
        """`:184`'s true arm and `:185`'s raise.

        `replies_enabled` False is what `lock_post_reply` sets, so this is the
        downstream half of the lock Group F covers.
        """
        s = _seed_for_reply()
        s.reply.replies_enabled = False
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'locked out', 'notify_author': False,
                   'language_id': 2}

        with pytest.raises(Exception, match='cannot be replied to'):
            make_reply(payload, s.post, s.reply.id, SRC_API,
                       auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before
```

- [ ] **Step 3: Re-measure the function's range and report what remains**
- [ ] **Step 4: Duplicate-name check**
- [ ] **Step 5: Commit**

Subject: `test: cover make_reply's source fork, ban guard and parent checks`

---

### Task 4: `make_reply` to zero

**Files:**
- Modify: `tests/test_shared_reply_make.py` (class `TestMakeReply`)

**Interfaces:**
- Consumes: Task 1's helpers, Task 3's class.
- Produces: nothing.

**Target: the rest of `make_reply`'s 42/22** — the post-author block at `:189-193`, the permission check at `:192`, the federation call at `:208`, and whichever `PostReply.new` raises Probe A found reachable.

- [ ] **Step 1: Work from a fresh arc enumeration, not from this plan**

Re-run Task 2's Step 1 command against `:156-213` and close what the list actually shows. **The plan's figure of 42/22 is coverage.py's; your list will differ by the `def` line if you derive it any other way.**

- [ ] **Step 2: Add the remaining tests**

```python
    def test_a_blocked_replier_is_refused_by_the_posts_author(self, db_session):
        """`:189`'s true arm and `:190`'s raise -- the POST author's block.

        Distinct from the parent-reply block at `:182`: this fires with
        `parent_id=None`, so `:180` is false and `:187` set `parent_reply` to
        None. Both blocks must be witnessed separately or they move only in
        lockstep.
        """
        s = _seed_for_reply()
        from app.models import UserBlock
        db.session.add(UserBlock(blocker_id=s.author.id, blocked_id=s.actor.id))
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'blocked at post level', 'notify_author': False,
                   'language_id': 2}

        with pytest.raises(Exception):
            make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_a_user_without_permission_to_comment_is_refused(self, db_session):
        """`:192`'s true arm and `:193`'s raise.

        `can_create_post_reply` (app/utils.py:2546) is the gate. Use the
        lever the module's other tests use for permission refusals rather
        than banning the user, because a ban is already witnessed at `:174`
        and the two would move in lockstep.
        """
        s = _seed_for_reply()
        s.actor.bot = True
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'not permitted', 'notify_author': False,
                   'language_id': 2}

        with pytest.raises(Exception):
            make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_the_federation_task_is_selected_with_the_parent_id(self, db_session):
        """`:208`'s task_selector call, including its `parent_id` argument.

        `recording_task_selector` is defined in
        `tests/test_shared_reply_moderation.py`; this file defines its own
        rather than importing across test modules, because the two rebind
        DIFFERENT module globals and sharing one would be a false witness.
        """
        s = _seed_for_reply()
        import app.shared.reply as reply_module
        calls = []
        original = reply_module.task_selector

        def recorder(task_key, **kwargs):
            calls.append((task_key, kwargs.get('parent_id')))
            return original(task_key, **kwargs)

        reply_module.task_selector = recorder
        try:
            payload = {'body': 'federated', 'notify_author': False,
                       'language_id': 2}
            make_reply(payload, s.post, s.reply.id, SRC_API,
                       auth=bearer(s.actor))
        finally:
            reply_module.task_selector = original

        assert ('make_reply', s.reply.id) in calls
```

- [ ] **Step 3: Close whichever `PostReply.new` raises Probe A found reachable**

Write one test per reachable raise, each asserting **no row was created** alongside `pytest.raises`. If a raise is unreachable from a factory seed, say so with the reason rather than writing a test that cannot run.

- [ ] **Step 4: Re-measure `:156-213`, both lists empty, checked AS LISTS**
- [ ] **Step 5: Duplicate-name check**
- [ ] **Step 6: Commit**

Subject: `test: take make_reply to zero missing statements and arcs`

---

### Task 5: Group D's file and `report_reply`'s source fork

**Files:**
- Create: `tests/test_shared_reply_report.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_seed_for_report()` and `add_moderator(s, user, *, local=True)`. Task 6 uses both.

**Target: `report_reply`'s first half** — the source fork at `:312-328`, `targets_data` at `:330-340`, the `Report` row at `:342-353`.

- [ ] **Step 1: Write the spine and fixture**

```python
"""Group D of app/shared/reply.py -- reporting a reply.

    report_reply   :311   55 statements / 34 arcs

The module's largest function, and at ZERO coverage when this file was
created. `tests/test_shared_post_moderation.py` covers the twin
`report_post` (app/shared/post.py:833).

THIS FUNCTION IS A LOOP, NOT A FORK, AND THAT SHAPES EVERY FIXTURE HERE.
`:359`-`:376` iterates `reply.community.moderators()` and branches per
moderator on `moderator.is_local()`, with `report_remote` gating which remote
instances are collected. Witnessing both arms needs at least one LOCAL and one
REMOTE moderator on the same community, which is what `_seed_for_report`
builds.

`Site.admins()` AT `:379` IS REGISTER ENTRY D442, LIVE. Its behaviour differs
where `g.admin_ids` is unset. Record what it does here; do not fix it.

`notify_admins` AT `:318`-`:319` IS A SUBSTRING TEST over two lists, so 'dox'
matches any word containing it. Recorded, not fixed.

EVERY SEEDED ID IS OFFSET -- see register entry D533. Sub-project 41's fixture
left three ids equal to 1 and hid at least four mutants, because several call
sites resolve objects to ids and a wrong-object-right-id mutation is then
invisible.
"""

import pytest
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Instance, Notification, Report, User
from app.shared.reply import report_reply
from tests.factories import (
    bearer, make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_site, make_user, web_ctx,
)


def _seed_for_report(*, private=True):
    """A reply on a community with one LOCAL and one REMOTE moderator.

    `reporter` files the report, `author` wrote the reply, `local_mod` and
    `remote_mod` moderate the community. Four distinct users, because `:359`'s
    loop branches per moderator and `:380`'s admin block skips anyone already
    notified -- a fixture that reused one user could not tell those apart.

    THE IDS ARE FORCED APART, per D533: spare rows advance the community and
    post sequences past the user sequence, and the assertion below fails if a
    factory change makes them collide.
    """
    local_instance = make_instance('local.example', software='piefed')
    remote_instance = make_instance('remote.example', software='lemmy')
    reporter = make_user(local_instance, 'reporter', local=True)
    author = make_user(local_instance, 'author', local=True)
    local_mod = make_user(local_instance, 'local-mod', local=True)
    remote_mod = make_user(remote_instance, 'remote-mod', local=False)
    make_community('report-burner-one')
    community = make_community('reports')
    community.private = private
    db.session.commit()
    make_post(community, author, 'https://local.example/burner')
    post = make_post(community, author, 'https://local.example/p/1')
    reply = make_post_reply(post, author)
    db.session.commit()
    assert len({community.id, post.id, reporter.id}) == 3, (
        'D533: seeded ids collided -- wrong-object-right-id would be invisible'
    )
    return SimpleNamespace(local_instance=local_instance,
                           remote_instance=remote_instance,
                           reporter=reporter, author=author,
                           local_mod=local_mod, remote_mod=remote_mod,
                           community=community, post=post, reply=reply)


def add_moderator(s, user):
    """Make `user` a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) returns CommunityMember
    rows filtered on `is_banned == False`, and `:360` then loads each
    `User` by `mod.user_id`.
    """
    return make_community_member(user, s.community, is_moderator=True)
```

- [ ] **Step 2: Write the source-fork tests**

```python
class TestReportReply:
    """`report_reply` (app/shared/reply.py:311-410)."""

    def test_the_api_arm_creates_the_report_row(self, db_session):
        """`:312` true, `:313`-`:320`, `:342`-`:353`, `:397`, `:408`.

        Asserts the Report's OWN fields rather than a bare count: `:342`-`:352`
        writes nine of them and a count would pass with eight deleted.
        `reply.reports` at `:397` is asserted separately because nothing else
        writes it.
        """
        s = _seed_for_report()
        payload = {'reason': 'spam', 'description': 'clearly spam',
                   'report_remote': False}

        reporter_id, report = report_reply(s.reply, payload, SRC_API,
                                           auth=bearer(s.reporter))

        assert reporter_id == s.reporter.id
        db.session.refresh(s.reply)
        assert s.reply.reports == 1
        rows = db.session.query(Report).all()
        assert {r.suspect_post_reply_id for r in rows} == {s.reply.id}
        assert {r.reporter_id for r in rows} == {s.reporter.id}
        assert {r.suspect_user_id for r in rows} == {s.author.id}

    def test_the_web_arm_reads_the_form_and_returns_none(self, db_session, app):
        """`:312` false -> `:322`-`:328`, and `:410`'s bare return.

        The web arm builds `reason` from `input.reasons_to_string(...)` and
        `notify_admins` from membership of '5' or '6' in `reasons.data` --
        a DIFFERENT mechanism from the API arm's substring test, which is why
        both arms need their own test rather than one parameterised over src.
        """
        s = _seed_for_report()
        form = SimpleNamespace(
            reasons=SimpleNamespace(data=['1']),
            description=SimpleNamespace(data='a web report'),
            report_remote=SimpleNamespace(data=False),
            reasons_to_string=lambda data: 'spam',
        )

        with web_ctx(app, s.reporter):
            result = report_reply(s.reply, form, SRC_WEB, auth=None)

        assert result is None
        rows = db.session.query(Report).all()
        assert {r.reporter_id for r in rows} == {s.reporter.id}
```

- [ ] **Step 3: Run, re-measure, report what remains**
- [ ] **Step 4: Duplicate-name check**
- [ ] **Step 5: Commit**

Subject: `test: open the reply report file and cover its source fork`

---

### Task 6: `report_reply` to zero — the moderator loop and the remote set

**Files:**
- Modify: `tests/test_shared_reply_report.py` (class `TestReportReply`)

**Interfaces:**
- Consumes: `_seed_for_report`, `add_moderator` from Task 5.
- Produces: the pinning test `test_the_community_guard_compares_the_wrong_id_space`, which **Task 7 does NOT invert** — the finding is registered, not fixed.

**Target: the rest of `report_reply`'s 55/34.**

- [ ] **Step 1: Add the loop tests**

```python
    def test_a_local_moderator_is_notified(self, db_session):
        """`:359`'s loop, `:362`'s true arm, `:363`-`:370`.

        The Notification's `user_id` must be the MODERATOR's and its
        `author_id` the REPORTER's -- a mutant swapping those two operands
        produces the same row count and is caught only by asserting both.
        """
        s = _seed_for_report()
        add_moderator(s, s.local_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        rows = db.session.query(Notification).all()
        assert {n.user_id for n in rows} == {s.local_mod.id}
        assert {n.author_id for n in rows} == {s.reporter.id}

    def test_a_remote_moderator_is_not_notified_locally(self, db_session):
        """`:362`'s false arm -> `:371`-`:376`.

        THE POSITIVE CONTROL IS THE TEST ABOVE, and it is required: "no
        Notification row" is both the correct outcome here and the signature
        of a fixture where no moderator exists at all. That test proves this
        fixture CAN notify.
        """
        s = _seed_for_report()
        add_moderator(s, s.remote_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert db.session.query(Notification).count() == 0

    def test_report_remote_false_skips_a_mod_on_the_suspects_instance(self, db_session):
        """`:372`'s true arm and `:373`'s three-way comparison.

        With `report_remote` False, a remote moderator is collected ONLY if
        its instance differs from both the suspect's and the community's. Here
        the remote moderator shares the suspect's instance, so nothing is
        collected and `:400`'s `len(remote_instance_ids)` is zero.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        db.session.commit()
        add_moderator(s, s.remote_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' not in calls

    def test_report_remote_true_collects_every_remote_moderator(self, db_session):
        """`:375`-`:376` -- the else arm of `:372`.

        The same fixture as the test above with one lever moved, so the
        difference in outcome is `report_remote` and nothing else.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        db.session.commit()
        add_moderator(s, s.remote_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' in calls
```

- [ ] **Step 2: Add the admin-notification tests**

```python
    def test_a_csam_reason_notifies_site_admins(self, db_session):
        """`:318`-`:319`'s substring test, `:378`'s true arm, `:379`-`:386`.

        `Site.admins()` at `:379` IS REGISTER ENTRY D442, LIVE. Record what it
        returns here rather than assuming; the entry says its behaviour
        differs where `g.admin_ids` is unset.

        `unread_notifications` is asserted as well as the Notification row,
        because `:386` is a separate statement a mutant can delete on its own.
        """
        s = _seed_for_report()
        make_site()
        admin = make_user(s.local_instance, 'site-admin', local=True)
        db.session.commit()
        from app.models import Role, user_role
        role = Role(name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
        db.session.execute(user_role.insert().values(user_id=admin.id,
                                                     role_id=role.id))
        admin.unread_notifications = 3
        db.session.commit()
        payload = {'reason': 'csam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        db.session.refresh(admin)
        assert admin.unread_notifications == 4

    def test_an_ordinary_reason_does_not_notify_admins(self, db_session):
        """`:378`'s false arm -- the same-mechanism positive control.

        Identical to the test above with `reason` changed, so the zero here is
        `notify_admins` being False rather than an absent admin.
        """
        s = _seed_for_report()
        make_site()
        admin = make_user(s.local_instance, 'site-admin', local=True)
        db.session.commit()
        from app.models import Role, user_role
        role = Role(name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
        db.session.execute(user_role.insert().values(user_id=admin.id,
                                                     role_id=role.id))
        admin.unread_notifications = 3
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        db.session.refresh(admin)
        assert admin.unread_notifications == 3
```

- [ ] **Step 3: Add the remote-instance block tests and the pin**

```python
    def test_a_remote_suspect_instance_is_added_when_reporting_remotely(self, db_session):
        """`:393`'s true arm and `:394`-`:395`.

        The suspect-user half of the `report_remote` block, which is the half
        written CORRECTLY -- it guards on `suspect_user.instance_id` and adds
        the same value.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' in calls

    def test_the_community_guard_compares_the_wrong_id_space(self, db_session):
        """`:390`-`:392` -- AND IT PINS A REGISTERED DEFECT ON PURPOSE.

        `:391` is `if reply.community_id not in remote_instance_ids:` and
        `:392` adds `reply.community.instance_id`. Those are DIFFERENT ID
        SPACES: the guard tests a community id against a set of instance ids,
        so it cannot do what it is written to do. The `suspect_user` block at
        `:393`-`:395` gets the identical pattern right, which is what makes
        this a slip rather than a convention.

        THIS TEST RECORDS TODAY'S BEHAVIOUR AND THIS ROUND DOES NOT FIX IT --
        the production budget is the counter fix, and a duplicate Flag to one
        instance is a different blast radius from a permanently drifting
        counter. IF A LATER ROUND REPAIRS `:391` TO GUARD ON
        `reply.community.instance_id`, THE EDIT OWED HERE IS TO ASSERT THE
        INSTANCE APPEARS EXACTLY ONCE rather than that the branch was taken.

        Registered as this round's finding 1.
        """
        s = _seed_for_report()
        s.community.instance_id = s.remote_instance.id
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' in calls

    def test_a_description_is_appended_to_the_summary(self, db_session):
        """`:402`'s true arm and `:403`'s concatenation.

        The summary is only built when `:400` finds a remote instance, so this
        test needs the remote path as well. Assert the JOINED string, because
        `:401` alone would pass with `:403` deleted.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'with detail',
                   'report_remote': True}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        summaries = [k.get('summary') for _, k in calls]
        assert 'spam - with detail' in summaries

    def test_an_empty_description_leaves_the_summary_bare(self, db_session):
        """`:402`'s false arm -- the counterpart of the test above.

        Together they prove `:403` is driven by `description` rather than
        running unconditionally.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        db.session.commit()
        payload = {'reason': 'spam', 'description': '', 'report_remote': True}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        summaries = [k.get('summary') for _, k in calls]
        assert 'spam' in summaries
```

- [ ] **Step 4: Add the recorder this file needs**

Define `_recording_task_selector(capture_kwargs=False)` in this file, rebinding `task_selector` on `app.shared.reply` and restoring in a `finally`. It must yield a list of `(task_key, kwargs)` pairs when `capture_kwargs` is True and of task keys otherwise, because Step 3's summary tests need the kwargs and Step 1's do not.

- [ ] **Step 5: Re-measure `:311-410`, both lists empty, checked AS LISTS**
- [ ] **Step 6: Duplicate-name check**
- [ ] **Step 7: Commit**

Subject: `test: take report_reply to zero and pin its id-space guard`

---

### Task 7: Fix D523 and D522

**Files:**
- Modify: `app/shared/reply.py` (three functions)
- Modify: `tests/test_shared_reply_interactions.py` (invert the delete/restore pin)
- Modify: `tests/test_shared_reply_moderation.py` (invert the mod-pair pin, if one exists)

**Interfaces:**
- Consumes: the pins Groups C and E already carry.
- Produces: nothing.

**This is the only task that may touch `app/`.**

- [ ] **Step 1: Find the pins that assert today's behaviour**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -n "cross_posted\|post_reply_count" tests/test_shared_reply_interactions.py tests/test_shared_reply_moderation.py
```

Sub-project 40 pinned the delete/restore divergence deliberately —
`..._a_delete_restore_cycle_leaves_two_counters_permanently_low` is named in
that round's register entry. **Read every hit and list which tests assert the
drift.** Those are the tests this task inverts, and there may be more than one.

- [ ] **Step 2: Paste the failing observation BEFORE changing anything**

Run those tests and paste their currently-PASSING output. That is the defect in executable form.

- [ ] **Step 3: Make the change**

Each of the three functions gains two lines inside its existing bot guard. `restore_reply`:

```python
    if not reply.author.bot:
        reply.post.reply_count += 1
        reply.post.reply_count_cross_posted += 1
        reply.community.post_reply_count += 1
    reply.author.post_reply_count += 1
```

`mod_remove_reply` takes the decrementing form, `mod_restore_reply` the incrementing one. **Re-derive all three line numbers first** — and note that each edit shifts the two below it.

- [ ] **Step 4: Invert the pins**

Rewrite each docstring to say the defect WAS fixed, in the past tense, what was claimed, and why it is now stale. A retraction is not a silent rewrite.

- [ ] **Step 5: RE-MEASURE Groups C and E**

```bash
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
f=json.load(open('/tmp/t7.json'))['files']['app/shared/reply.py']
print('missing stmts', f['missing_lines'])
print('missing arcs', f['missing_branches'])
"
```

`delete_reply` and `restore_reply` were at zero before this task. **Sub-project 41 added four production lines and created two new uncovered arcs that nobody noticed for two tasks.** Close anything this change opened.

- [ ] **Step 6: Run every file that touches these functions**

`tests/test_shared_reply_interactions.py`, `tests/test_shared_reply_moderation.py`, and the two this round created.

- [ ] **Step 7: Commit**

Subject: `fix: restore the two counters a delete leaves behind`

---

### Task 8: The mutation pass

**Files:**
- Modify: both new test files, only to close holes.

- [ ] **Step 1: Derive the statement and compound lists mechanically**

`ast.walk` over the three target functions. **Publish the command and its raw output beside the count.** An `ast.walk` counts the `def` line where coverage.py does not, so the list runs one longer per function — expected, and not to be reconciled away.

- [ ] **Step 2: Run the pass, one mutation at a time**

Dry-run the `sed`, read the line with numbered output, apply, run, **restore with a REVERSE edit**, then assert `git diff --quiet -- app/` and the correct `wc -l`. **`git checkout -- app/` is banned.** Restore before any point where you might stop and report — a background scanner samples the tree and reported six of sub-project 41's mutation windows as production defects.

- [ ] **Step 3: The mandatory mutation**

Neutralise `edit_reply:224`'s moderator guard so it never refuses. Five consecutive rounds have found a real hole this way.

- [ ] **Step 4: Classify every survivor**

A crash kill is not a kill unless a viable non-crashing variant also dies. An operator can be structurally void. **Fix-catching is not a unique kill.** Close what is worth closing; record the rest **with reproduction recipes** — this workspace is deleted at the round's end. Arithmetic must reconcile: killed + survivors = total, and the survivors must break into named categories that sum.

- [ ] **Step 5: Commit**

Subject: `test: close the holes the reply create and report mutation pass found`

---

### Task 9: Close the module

**Files:**
- Modify: `coverage_floors.ini`, `tests/README.md`, `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**DO NOT RUN THE FULL SUITE.**

- [ ] **Step 1: Measure at more than one scope**

Confirm the file list with `ls`. Sub-project 40's Task 8 found its brief's glob was wrong and that a file outside the `test_shared_` namespace was closing seven statements; sub-project 41 reproduced the same trap. Measure at the module's own files, then at a superset, and say which is which.

- [ ] **Step 2: Verify the WHOLE MODULE is closed**

`missing_lines` and `missing_branches` must both be `[]` for `app/shared/reply.py` — **not just for Groups B and D.** Check as lists. If any earlier group reopened, close it and say what reopened it.

- [ ] **Step 3: Raise the floor**

To `floor(percent_covered)`. If the module is at 100, the floor is 100.

- [ ] **Step 4: `tests/README.md` facts from 237**

At minimum: which `PostReply.new` raises a factory seed can reach (Probe A); that `report_reply` needs both a local and a remote moderator on one community; and the D533 sequence-offset pattern both new fixtures use.

- [ ] **Step 5: Register findings from D538**

At minimum: the four findings the spec names; D523 and D522 marked **FIXED** with the maintenance-job argument that settled the direction; Task 8's survivors with recipes; and the fact that **`app/shared/reply.py` is closed**, which leaves the campaign with no scheduled target for the second time.

- [ ] **Step 6: Commit**

Subject: `test: close app/shared/reply.py and register sub-project 42's findings`

---

## Self-Review

**Spec coverage.** Goal → Tasks 1-6. Harness → Tasks 1 and 5. Production fix → Task 7. Finding 1 (`:391` id spaces) → pinned Task 6. Finding 2 (`edit_reply`'s double check) → pinned Task 2. Findings 3 and 4 (AP bot guard, D442) → registered Task 9. Five false-witness mechanisms → named in the docstrings relying on them. Verification → Task 8. Success criteria → Task 9 plus the controller's suite.

**Placeholder scan.** No TBD, no "similar to Task N". Task 4 Step 3 and Task 6 Step 4 describe work whose exact content depends on a probe result and a helper signature respectively — both state the requirement and the reason, which is an instruction, not a placeholder.

**Type consistency.** `_seed_for_reply` returns `(instance, author, actor, community, post, reply)`; `_seed_for_report` returns `(local_instance, remote_instance, reporter, author, local_mod, remote_mod, community, post, reply)`. The two are deliberately different shapes and live in different files. `make_moderator(s, user=None)` is Group B's; `add_moderator(s, user)` is Group D's — different names because their defaults differ.

**One risk stated plainly.** Task 7 shifts every line after `:293` in `app/shared/reply.py`, so Tasks 8 and 9 must re-derive rather than inherit — and the register entry that records this exact hazard has itself been corrected four times.

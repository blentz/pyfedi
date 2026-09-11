# Coverage sub-project 34 Implementation Plan: `post.py` Group A

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the eight reader-facing functions of `app/shared/post.py` to zero missing statements and zero missing arcs, and fix three production defects found while reading them.

**Architecture:** One new test file, `tests/test_shared_post_interactions.py`, built on a request-context-plus-login harness that no earlier round in this campaign needed. Tasks 1-6 and 8 add coverage against the tree as it stands; tasks 7, 9 and 10 each observe a production defect failing, fix it, and cover the new arms. Task 11 is the mutation pass, task 12 the measurement, floor raise and register.

**Tech Stack:** pytest, Flask, Flask-Login, SQLAlchemy 2.0.52, Celery (eager), fakeredis, coverage.py with branch measurement. Everything runs through `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-10-coverage-post-a-34-design.md`

## Global Constraints

- **Delete nothing this plan did not create.** `git checkout -- app/` is permitted ONLY as a mutation-restore step.
- **There is NO host Python with flask or pytest.** Everything runs through `./run_tests.sh`, which is `podman compose exec -T test-runner pytest "$@"` (`run_tests.sh:85`).
- **Only the controller runs the full suite**, one pytest session at a time, in the foreground.
- **`pytest` exits 1 on a session timeout and `run_tests.sh` propagates it.** A shell pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.
- **Coverage takes the dotted module form**: `--cov=app.shared.post`. A path form collects nothing, writes no JSON, and exits 0 — a silent green failure.
- **Write coverage JSON outside the repository.** `/app` is bind-mounted; use `--cov-report=json:/tmp/<name>.json`.
- **Read `summary.percent_covered`**, not `percent_statements_covered`. `tests/check_coverage_floors.py:75` reads the former, which is combined statement+branch.
- **Test counts come from pytest's own collection output**, never `grep -c '^def test_'`.
- Before believing any failure, run `./run_tests.sh --down`.
- **Mutations: one at a time.** Dry-run without `-i` and read the produced line first, apply, run, restore, then assert an empty `git diff -- app/` and the expected `wc -l`. **Restore before any point where you might stop and report.** Paste every dry-run line and every failure.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- **Every line number must be re-derived against the current tree with numbered output**: `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. Never count lines of an unnumbered `sed -n 'X,Yp'` range.
- **Where prose describes a statement, cite that statement's line** — not the `if` guarding it, not the `def` above it.
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, normal English prose.
- Commit trailers, last two lines in this order:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Do not run any probe that holds an uncommitted write on a row another session touches.** It deadlocks Postgres and wedges the stack.
- **Do not dispatch subagents.** Review arrives from the controller.
- **Write scope:** each task writes only `tests/test_shared_post_interactions.py`, the production lines its own task names, and its own report file. Nothing else. In particular, do not create files in the repository root.
- **Register numbering:** findings start at **D392**, `tests/README.md` facts start at **206**.

---

## File Structure

**Create:** `tests/test_shared_post_interactions.py` — the whole round's test surface. One file, matching the campaign's one-file-per-round convention (`tests/test_shared_tasks_maintenance_identity.py`, `..._health.py`, and three siblings).

**Modify:** `app/shared/post.py` at three sites only — `vote_for_post:53`/`:55` (Task 7), `vote_for_poll`'s two voting arms (Tasks 9 and 10). No other production file changes.

**Modify:** `coverage_floors.ini` (Task 12), `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (Task 12), `tests/README.md` (Task 12).

---

## Naming hazard to know before you start

`tests/factories.py:665` defines `mark_post_read(user: User, post: Post) -> None`, a factory that inserts one `read_posts` row. `app/shared/post.py:1115` defines `mark_post_read(post_ids: List[int], read: bool, user_id: int)`, the production function under test. **They have the same name and incompatible signatures.**

`tests/factories.py:679` defines `hide_post(user, post)` and `app/shared/post.py:1020` defines `hide_post(post_id, hidden, src, auth=None)` — the same collision, though `hide_post` is Group B and out of this round's scope.

This file imports the production one plainly and never imports the factory one:

```python
from app.shared.post import mark_post_read
```

If a test needs a pre-seeded read row, insert it through the table directly (Task 2 shows how) rather than importing the factory.

---

## Task 1: Open the file, build the harness, and probe five unknowns

**Files:**
- Create: `tests/test_shared_post_interactions.py`
- Test: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Produces: `_seed()` returning a `SimpleNamespace` with attributes `instance`, `site`, `author`, `voter`, `community`, `post`; and `_web_ctx(app, user)`, a context manager yielding inside a request context with `user` logged in. Tasks 2-10 all consume both.

This task is a **probe**, not a coverage task. Its deliverable is a working harness plus five recorded answers. A surprising answer here is worth more than the prediction it contradicts — if a probe contradicts this plan, say so in the report and the controller rules on it.

- [ ] **Step 1: Read the spec's harness section**

Read `docs/superpowers/specs/2026-09-10-coverage-post-a-34-design.md`, the section titled "The harness is this round's real cost, and it is new". It states five things this task must confirm rather than assume.

- [ ] **Step 2: Re-derive every line number this task cites**

```bash
awk 'NR>=1115 && NR<=1143 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
awk 'NR>=155 && NR<=161 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Expected: `:1126` is `from app import redis_client`, `:1134` is `if isinstance(post, int):`, `:1137` is `if not post.flair:`, `:1160` is `return False` inside `extra_rate_limit_check`. If any differs, the tree has moved and every citation in this plan needs re-deriving before you continue. Report that rather than silently adjusting.

- [ ] **Step 3: Write the file's header, helpers, and the two auth-free functions' tests**

Create `tests/test_shared_post_interactions.py`:

```python
"""`app/shared/post.py`'s reader interactions -- Group A of five.

SCOPE. The eight reader-facing functions, 63 uncovered statements and 51
uncovered branch arcs when this file was started:

  vote_for_post :31-74 (29/18), vote_for_poll :1146-1174 (20/18),
  bookmark_post :77-94 (4/4), subscribe_post :115-152 (4/4),
  mark_post_read :1115-1130 (2/3), get_post_flair_list :1133-1143 (2/2),
  remove_bookmark_post :97-112 (1/2), extra_rate_limit_check :155-160 (1/0).

THE HARNESS IS THE POINT OF THIS FILE, and it is not the harness the four
maintenance test files use. Those call Celery task bodies directly: no request,
no user, no session. Nothing here can do that.

NO `user=` ESCAPE HATCH. tests/test_shared_post_edit.py runs without login or
request context because `edit_post` takes `user=` and checks `if not user:` on
both source arms (:252 and :316). No Group A function has that parameter.
vote_for_post:41, bookmark_post:78, remove_bookmark_post:98, subscribe_post:117
and vote_for_poll:1150 read `current_user` or call `authorise_api_user` with no
way around it.

THE SRC_API ARM NEEDS A REQUEST CONTEXT TOO. This is the fact most easily
missed. vote_for_post:50 and vote_for_poll:1152 both read
`if user.banned or user_ip_banned():` AFTER the source fork, so both arms reach
it. `user_ip_banned` (app/utils.py:2311-2314) calls `ip_address`, which
app/utils.py:2308 binds to app/__init__.py's `get_ip_address`, and that reads
`request`. There is no context-free path through either function. A test that
mints a bearer token and calls without a request context fails inside
`user_ip_banned`, not at its assertion.

THE FEDERATION LEVER IS `community.private`, NOT `local_only`. task_selector
runs synchronously under eager Celery (tests/conftest.py:105-110), so
vote_for_post:60 and vote_for_poll:1165/:1173 execute real bodies in
app/shared/tasks/likes.py. Both bodies return at their first guard --
`send_vote:60` and the poll task's own equivalent -- when
`community.local_only or community.private or not community.instance.online()`.
`local_only` looks like the obvious lever and is the WRONG one: `can_downvote`
(app/utils.py) returns False for `community.local_only and not user.is_local()`,
so setting it silently disables every downvote test. `private` is checked by the
federation guard and by nothing on the voting path.

NAME COLLISION. tests/factories.py:665 has its own `mark_post_read(user, post)`,
a two-argument factory, and app/shared/post.py:1115 has the three-argument
production function. This file imports the production one and never the factory.
tests/factories.py:679's `hide_post` collides the same way with
app/shared/post.py:1020, which is Group B.
"""

import pytest
from contextlib import contextmanager
from types import SimpleNamespace

from flask_login import login_user

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Poll, PollChoice, PollChoiceVote, PostBookmark, \
    NotificationSubscription, PostVote, read_posts
from app.shared.post import (
    bookmark_post,
    extra_rate_limit_check,
    get_post_flair_list,
    mark_post_read,
    remove_bookmark_post,
    subscribe_post,
    vote_for_poll,
    vote_for_post,
)
from tests.factories import make_community, make_instance, make_post, \
    make_post_flair, make_site, make_user


def _seed(*, private=True):
    """One instance, one site, one community, an author, a voter and a post.

    `private=True` is the federation lever described in the module docstring: it
    stops app/shared/tasks/likes.py's eager task bodies at their first guard so
    no test issues an outbound request. Pass private=False only in a test that
    means to exercise the federation path, and expect to arrange for it.

    `make_community` hardcodes `instance_id=1` (tests/factories.py:141) and
    tests/conftest.py:131 resets sequences after every test, so the instance
    seeded first here lands on id 1 and the community resolves to it. Unlike the
    maintenance rounds, this round WANTS id 1 rather than avoiding it.
    """
    instance = make_instance('local.example', software='piefed')
    site = make_site()
    author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    community = make_community('interactions')
    community.private = private
    db.session.commit()
    post = make_post(community, author, 'https://local.example/p/1')
    return SimpleNamespace(instance=instance, site=site, author=author,
                           voter=voter, community=community, post=post)


@contextmanager
def _web_ctx(app, user, query_string=''):
    """A request context with `user` logged in, for the SRC_WEB arms.

    `query_string` feeds `request.args`, which vote_for_post:45 and :72 read as
    `request.args.get('style', '')` to choose between two templates.
    """
    with app.test_request_context('/?' + query_string):
        login_user(user)
        yield


def _bearer(user):
    """The Authorization header value the SRC_API arms authorise from.

    The precedent is tests/test_shared_post_edit.py:300, which passes
    `auth=f'Bearer {s.user.encode_jwt_token()}'` into edit_post's API branch.
    """
    return f'Bearer {user.encode_jwt_token()}'


def test_extra_rate_limit_check_returns_false_for_any_user(db_session):
    """`:160`'s `return False`, the function's whole body.

    Catches a regression making the stub return anything truthy. Its only
    production caller is make_post:166, which is Group D, so this round reaches
    it by direct call and says so rather than pretending otherwise.
    """
    s = _seed()

    assert extra_rate_limit_check(s.voter) is False


def test_flair_list_loads_the_post_when_given_an_integer(db_session):
    """`:1134`'s true arm and `:1135`'s lookup.

    Catches a regression dropping the isinstance branch, which would leave an
    int bound to `post` and fail at `:1137`'s attribute read.
    """
    s = _seed()
    make_post_flair(s.post, name='news')

    flair_list = get_post_flair_list(s.post.id)

    assert {f.id for f in flair_list} == {f.id for f in s.post.flair}
    assert len(flair_list) == 1


def test_flair_list_accepts_a_post_object_and_skips_the_lookup(db_session):
    """`:1134`'s false arm.

    Pins that a Post instance is used as given. Catches a regression that made
    the lookup unconditional, which would raise on a Post argument.
    """
    s = _seed()
    make_post_flair(s.post, name='news')

    flair_list = get_post_flair_list(s.post)

    assert len(flair_list) == 1


def test_flair_list_returns_an_empty_list_when_the_post_has_no_flair(db_session):
    """`:1139`'s `flair_list = []`, reached through `:1137`'s true arm.

    Catches a regression returning `post.flair` unguarded, which would hand the
    caller None instead of a list.
    """
    s = _seed()

    flair_list = get_post_flair_list(s.post)

    assert flair_list == []
```

- [ ] **Step 4: Run the new tests**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 4 passed. `make_post_flair` (`tests/factories.py:710-723`) returns a `CommunityFlair` and appends it to `post.flair`, so comparing id sets is the right shape — there is no `PostFlair` model.

If a test errors on an import, on `make_site()`, or on `community.private`, that is a harness finding: record the exact error text in the report before adjusting.

- [ ] **Step 5: Probe A — does `redis_double` reach a function-body redis import?**

`mark_post_read:1126` is `from app import redis_client` written inside the function body, and `:1127` locks on it. `tests/conftest.py:459-463` warns that its `redis_double` fixture covers `app.redis_client` but that at least one `from app import redis_client` site does that import inside a function rather than at module level.

Add this test temporarily and run it:

```python
def test_probe_mark_post_read_reaches_a_working_redis(db_session, redis_double):
    s = _seed()

    mark_post_read([s.post.id], True, s.voter.id)

    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert len(rows) == 1
```

Run: `./run_tests.sh tests/test_shared_post_interactions.py::test_probe_mark_post_read_reaches_a_working_redis -v`

Record in the report: whether it passes, whether it passes **without** the `redis_double` fixture, and the exact error text if either fails. `delete_post:765` and `votes_cast_today` (`app/models.py`) do the same function-body import, so Groups B and C need this answer.

Delete the probe test after recording; Task 2 writes the real one.

- [ ] **Step 6: Probe B — do the three WEB templates render?**

`vote_for_post:47` and `:73` render `post/_post_voting_buttons.html` or `post/_post_voting_buttons_masonry.html`; `subscribe_post:152` renders `post/_post_notification_toggle.html`. All three files exist under `app/templates/post/`. They are Jinja templates over real model objects, so a missing `g` attribute or an unseeded relation surfaces as a template error, not an assertion failure.

Add this test temporarily and run it:

```python
def test_probe_the_notification_toggle_template_renders(db_session, app, redis_double):
    s = _seed()

    with _web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, True, SRC_WEB)

    assert isinstance(result, str)
```

Run: `./run_tests.sh tests/test_shared_post_interactions.py::test_probe_the_notification_toggle_template_renders -v`

Record the outcome and, on failure, the exact exception and the template line it names. If a template needs `g.site` or a `Site` row the seed does not create, note precisely what it needs — Task 4 and Tasks 5-6 both depend on it.

Delete the probe test after recording.

- [ ] **Step 7: Probe C — do the eager `likes.py` bodies return early with `private=True`?**

Add this test temporarily and run it:

```python
def test_probe_a_vote_does_not_reach_the_network(db_session, app, redis_double):
    s = _seed(private=True)

    with _web_ctx(app, s.voter):
        vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 1
```

Run: `./run_tests.sh tests/test_shared_post_interactions.py::test_probe_a_vote_does_not_reach_the_network -v`

`tests/conftest.py:262`'s session-scoped autouse `block_outbound_http` fails loudly on any real request, so a pass here is evidence the guard held. Record whether it passed, and if it did not, whether the failure came from `block_outbound_http`, from a template, or from somewhere else.

Delete the probe test after recording.

- [ ] **Step 8: Probe D — confirm the SRC_API arm needs a request context**

Add this test temporarily and run it:

```python
def test_probe_the_api_arm_needs_a_request_context(db_session, app, redis_double):
    s = _seed()

    with pytest.raises(Exception) as excinfo:
        vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                      auth=_bearer(s.voter))

    assert 'request' in str(excinfo.value).lower() or \
           'context' in str(excinfo.value).lower()
```

Run it. **The point is the exception's text, not the assertion.** Paste the verbatim exception into the report. If it turns out the API arm does NOT need a request context, that contradicts the spec and every later task changes shape — say so plainly rather than working around it.

Delete the probe test after recording.

- [ ] **Step 9: Probe E — what does `authorise_api_user` need?**

Add this test temporarily and run it:

```python
def test_probe_a_bearer_token_authorises(db_session, app, redis_double):
    s = _seed()

    with _web_ctx(app, s.voter):
        result = bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    assert result == s.voter.id
```

Note this deliberately runs the API arm inside a request context — Probe D's expected answer is that it must. Record whether the bearer token authorises, and the exact error if it does not (a missing `SECRET_KEY`, an unverified user, a `Site` row, a banned flag).

Delete the probe test after recording.

- [ ] **Step 10: Run the four real tests once more, clean**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 4 passed, no probe tests remaining. Confirm with `git diff --stat` that only `tests/test_shared_post_interactions.py` changed.

- [ ] **Step 11: Write the probe report**

Write to the report path the dispatch names. It must answer, in order: (A) does `redis_double` reach the function-body import, and is the fixture required; (B) do the templates render and what do they need; (C) do the eager task bodies return early with `private=True`; (D) does the SRC_API arm need a request context, with the verbatim exception; (E) does the bearer token authorise, and under what conditions.

Any answer that contradicts the module docstring means the docstring is wrong. Fix the docstring in this task and say you did.

- [ ] **Step 12: Commit**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: open the post interactions file and probe its harness`

---

## Task 2: `mark_post_read`

**Files:**
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `read_posts` (already imported by Task 1).
- Produces: nothing later tasks need beyond the tests themselves.

Target: `:1115-1130`, 2 missing statements and 3 missing arcs. The two statements are the two `db.session.execute` calls at `:1118-1120` and `:1123-1125`; the arcs are `:1116`'s fork plus both loops' zero-iteration exits.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=1115 && NR<=1130 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:1116` is `if read is True:`, `:1118` begins the INSERT, `:1123` begins the DELETE, `:1126` is the redis import, `:1130` is `db.session.commit()`.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_shared_post_interactions.py`:

```python
def test_marking_read_inserts_one_row_per_post_id(db_session, redis_double):
    """`:1118-1120`'s INSERT, reached through `:1116`'s true arm.

    Catches a regression inverting `:1116`, which would send a read=True call
    down the DELETE branch and leave the table empty.
    """
    s = _seed()
    second = make_post(s.community, s.author, 'https://local.example/p/2')

    mark_post_read([s.post.id, second.id], True, s.voter.id)

    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.read_post_id for row in rows} == {s.post.id, second.id}


def test_marking_read_twice_updates_rather_than_duplicating(db_session, redis_double):
    """`:1119`'s `ON CONFLICT (user_id, read_post_id) DO UPDATE`.

    Catches a regression dropping the conflict clause, which would raise a
    unique-violation on the second call instead of refreshing `interacted_at`.
    """
    s = _seed()

    mark_post_read([s.post.id], True, s.voter.id)
    first = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchone()
    mark_post_read([s.post.id], True, s.voter.id)

    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert len(rows) == 1
    assert rows[0].interacted_at >= first.interacted_at


def test_marking_unread_deletes_the_row(db_session, redis_double):
    """`:1123-1125`'s DELETE, reached through `:1116`'s false arm.

    Catches a regression inverting `:1116`, which would re-insert on a
    read=False call instead of removing.
    """
    s = _seed()
    mark_post_read([s.post.id], True, s.voter.id)

    mark_post_read([s.post.id], False, s.voter.id)

    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []


def test_an_empty_post_id_list_still_bumps_last_seen(db_session, redis_double):
    """Both loops' zero-iteration exit arcs, plus `:1128-1130`.

    `:1117` and `:1122` each need a zero-length list to record their exit arc,
    and `:1128`'s UPDATE runs regardless of how many posts were named -- so an
    empty call is not a no-op. Catches a regression moving the last_seen update
    inside either loop, which would make it depend on the list being non-empty.
    """
    s = _seed()
    s.voter.last_seen = None
    db.session.commit()

    mark_post_read([], True, s.voter.id)

    db.session.refresh(s.voter)
    assert s.voter.last_seen is not None
    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []


def test_an_empty_post_id_list_on_the_unread_branch_also_bumps_last_seen(db_session, redis_double):
    """`:1122`'s zero-iteration exit arc specifically.

    The read=True twin above records `:1117`'s exit arc; this records `:1122`'s.
    Coverage treats them as separate arcs and one test cannot take both.
    """
    s = _seed()
    s.voter.last_seen = None
    db.session.commit()

    mark_post_read([], False, s.voter.id)

    db.session.refresh(s.voter)
    assert s.voter.last_seen is not None
```

- [ ] **Step 3: Run them**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 9 passed. If `last_seen` is non-nullable and the `None` assignment fails, set it to a fixed past datetime instead and assert it moved forward — record the change.

- [ ] **Step 4: Confirm each test can fail**

For each of the five, state in the report the single-line regression it catches and the arm it pins. If any test would still pass with its named regression applied, fix the test now — this campaign has shipped roughly ten tests that could not fail, every one of them an oracle reached through a crash rather than through the guard its docstring named.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: cover mark_post_read's read and unread branches`

---

## Task 3: `bookmark_post` and `remove_bookmark_post`

**Files:**
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `_web_ctx(app, user)`, `_bearer(user)`.

Target: `:77-94` (4 statements, 4 arcs) and `:97-112` (1 statement, 2 arcs). The forks are `:78`'s source ternary, `:83`/`:101`'s existing-bookmark check, `:88`/`:106`'s API-versus-WEB split inside the duplicate arm, and `:93`/`:111`'s return fork.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=77 && NR<=113 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:78` is the `authorise_api_user(auth) if src == SRC_API else current_user.id` ternary, `:80` the `mark_post_read` call, `:83` `if not existing_bookmark:`, `:89` the `raise Exception(msg)`, `:91` the `flash(_(msg))`.

- [ ] **Step 2: Write the failing tests**

Append:

```python
def test_bookmarking_through_the_api_creates_the_row_and_marks_read(db_session, app, redis_double):
    """`:78`'s API arm, `:84`'s add, `:80`'s mark_post_read call, `:94`'s return.

    Pins all four at once because they lie on one straight path. Catches a
    regression dropping `:80`, which would leave the read_posts table empty
    while the bookmark still appeared.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1
    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.read_post_id for row in rows} == {s.post.id}


def test_bookmarking_through_the_web_reads_current_user_and_returns_none(db_session, app, redis_double):
    """`:78`'s WEB arm and `:93`'s false arm.

    `:93`'s `if src == SRC_API:` has no else, so the WEB call returns None. That
    is the arm this pins. Catches a regression making `:78` read the bearer
    token unconditionally, which would raise with auth=None.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = bookmark_post(s.post.id, SRC_WEB)

    assert result is None
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1


def test_bookmarking_twice_through_the_api_raises(db_session, app, redis_double):
    """`:83`'s false arm and `:89`'s raise, reached through `:88`'s true arm.

    Catches a regression inverting `:83`, which would add a second row rather
    than reject. Asserts the row count stayed at one so the raise is not
    reached through an unrelated crash.
    """
    s = _seed()
    with _web_ctx(app, s.voter):
        bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    with _web_ctx(app, s.voter):
        with pytest.raises(Exception, match='already been bookmarked'):
            bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1


def test_bookmarking_twice_through_the_web_flashes_instead_of_raising(db_session, app, redis_double):
    """`:88`'s false arm and `:91`'s flash.

    The WEB duplicate path must NOT raise -- that asymmetry is the arm. Catches
    a regression hoisting the raise out of `:88`, which would give the web route
    an exception it has no handler for.
    """
    s = _seed()
    with _web_ctx(app, s.voter):
        bookmark_post(s.post.id, SRC_WEB)

    with _web_ctx(app, s.voter):
        result = bookmark_post(s.post.id, SRC_WEB)

    assert result is None
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 1


def test_removing_a_bookmark_through_the_api_deletes_it(db_session, app, redis_double):
    """`:101`'s true arm, `:102`'s delete, `:112`'s return.

    Catches a regression inverting `:101`, which would leave the row and flash
    or raise instead.
    """
    s = _seed()
    with _web_ctx(app, s.voter):
        bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    with _web_ctx(app, s.voter):
        result = remove_bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostBookmark).filter_by(
        post_id=s.post.id, user_id=s.voter.id).count() == 0


def test_removing_a_bookmark_that_does_not_exist_raises_through_the_api(db_session, app, redis_double):
    """`:101`'s false arm and `:107`'s raise, through `:106`'s true arm.

    Catches a regression that made the delete unconditional, which would raise
    a different error entirely on a None row.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        with pytest.raises(Exception, match='was not bookmarked'):
            remove_bookmark_post(s.post.id, SRC_API, auth=_bearer(s.voter))


def test_removing_a_bookmark_that_does_not_exist_flashes_on_the_web(db_session, app, redis_double):
    """`:106`'s false arm and `:109`'s flash, plus `:111`'s false arm.

    Catches a regression hoisting `:107`'s raise out of `:106`.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = remove_bookmark_post(s.post.id, SRC_WEB)

    assert result is None
```

- [ ] **Step 3: Run them**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 16 passed.

`flash()` needs a session; `_web_ctx` provides a request context, which Flask's `flash` requires. If a flash call raises outside a session, wrap the seed app config or use `app.test_request_context` with a secret key already configured — record whatever you had to do.

- [ ] **Step 4: State each test's regression and arm in the report**

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: cover the post bookmark and unbookmark branches`

---

## Task 4: `subscribe_post`

**Files:**
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `_web_ctx(app, user)`, `_bearer(user)`, `NotificationSubscription`.

Target: `:115-152`, 4 statements and 4 arcs. The forks are `:119`'s `SRC_WEB` override of the caller's `subscribe` argument, `:124`'s `subscribe == False`, `:125` and `:136`'s existing-notification checks, `:130` and `:138`'s API-versus-WEB splits, and `:149`'s return fork — whose WEB arm renders `post/_post_notification_toggle.html`.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=115 && NR<=153 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:116` is the `.one()` lookup, `:119` `if src == SRC_WEB:`, `:124` `if subscribe == False:`, `:136` `if existing_notification:`, `:152` the `render_template` call.

- [ ] **Step 2: Write the failing tests**

Append:

```python
def test_subscribing_through_the_api_creates_the_subscription(db_session, app, redis_double):
    """`:143-147`'s creation, reached through `:124`'s false arm and `:136`'s
    false arm, and `:150`'s return.

    Catches a regression inverting `:136`, which would reject a first
    subscription as already existing.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


def test_subscribing_twice_through_the_api_raises(db_session, app, redis_double):
    """`:136`'s true arm and `:139`'s raise, through `:138`'s true arm.

    Asserts the count stayed at one, so the raise is not reached through an
    unrelated crash that would satisfy pytest.raises just as well.
    """
    s = _seed()
    with _web_ctx(app, s.voter):
        subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    with _web_ctx(app, s.voter):
        with pytest.raises(Exception, match='already existed'):
            subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


def test_unsubscribing_through_the_api_deletes_the_subscription(db_session, app, redis_double):
    """`:124`'s true arm, `:125`'s true arm, `:126`'s delete.

    Catches a regression changing `:124` to a truthiness test, which would send
    `subscribe=False` down the creation branch.
    """
    s = _seed()
    with _web_ctx(app, s.voter):
        subscribe_post(s.post.id, True, SRC_API, auth=_bearer(s.voter))

    with _web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, False, SRC_API, auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0


def test_unsubscribing_when_none_exists_raises_through_the_api(db_session, app, redis_double):
    """`:125`'s false arm and `:131`'s raise, through `:130`'s true arm."""
    s = _seed()

    with _web_ctx(app, s.voter):
        with pytest.raises(Exception, match='did not exist'):
            subscribe_post(s.post.id, False, SRC_API, auth=_bearer(s.voter))


def test_unsubscribing_when_none_exists_flashes_on_the_web(db_session, app, redis_double):
    """`:130`'s false arm and `:133`'s flash, plus `:152`'s render.

    The web arm must not raise. Catches a regression hoisting `:131` out of
    `:130`, which would give app/post/routes.py an unhandled exception.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, False, SRC_WEB)

    assert isinstance(result, str)


def test_the_web_arm_ignores_the_subscribe_argument_it_was_given(db_session, app, redis_double):
    """`:119-120`'s override, the function's least obvious behaviour.

    `:120` recomputes `subscribe` from `post.notify_new_replies(user_id)`, so a
    SRC_WEB caller passing subscribe=False when no subscription exists still
    CREATES one -- the argument is discarded. Catches a regression deleting
    `:119`, which would make the web arm honour the argument and silently change
    the toggle route's behaviour.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, False, SRC_WEB)

    assert isinstance(result, str)
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 1


def test_the_web_arm_removes_an_existing_subscription_when_toggled(db_session, app, redis_double):
    """`:120`'s other arm: with a subscription present, `notify_new_replies`
    returns truthy and `subscribe` becomes False, so the toggle removes.

    Together with the test above this exercises both values `:120` can produce.
    Catches a regression inverting `:120`'s ternary, which would make the toggle
    one-way.
    """
    s = _seed()
    with _web_ctx(app, s.voter):
        subscribe_post(s.post.id, True, SRC_WEB)

    with _web_ctx(app, s.voter):
        result = subscribe_post(s.post.id, True, SRC_WEB)

    assert isinstance(result, str)
    assert db.session.query(NotificationSubscription).filter_by(
        entity_id=s.post.id, user_id=s.voter.id).count() == 0
```

- [ ] **Step 3: Run them**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 23 passed.

If `post/_post_notification_toggle.html` fails to render, Task 1's Probe B recorded what it needs — apply that. If Probe B did not reach this template, record the template error verbatim and arrange the minimum the template requires.

- [ ] **Step 4: Verify the two `:120` tests genuinely take different arms**

`test_the_web_arm_ignores_the_subscribe_argument_it_was_given` must reach `:120` with no subscription present, and `test_the_web_arm_removes_an_existing_subscription_when_toggled` with one present. Confirm by checking `NotificationSubscription` count before each call, and state the confirmation in the report.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: cover subscribe_post including the web arm's argument override`

---

## Task 5: `vote_for_post`, the source fork and permission gates

**Files:**
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `_web_ctx(app, user, query_string='')`, `_bearer(user)`.

Target: `:32`'s source fork, `:35` and `:37`'s API-side gates on both arms, `:43-44`'s compound on the WEB side, and `:45`'s style ternary on both arms.

**`:43-44` is a disjunction of two conjunctions and coverage.py records it as a single arc pair.** Four distinct inputs are needed to exercise its parts, and only Task 11's mutation pass can see inside it. Write the four anyway — coverage will not tell you if you skip one.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=31 && NR<=75 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:32` is `if src == SRC_API:`, `:35` the API upvote gate, `:37` the API downvote gate, `:43-44` the WEB compound, `:45` the style ternary, `:47` the render.

- [ ] **Step 2: Understand the two permission helpers before writing fixtures**

`can_upvote(user, community)` (`app/utils.py`) returns False when the user is None, banned or a bot, or when the community id appears in `communities_banned_from(user.id)`.

`can_downvote(user, community)` returns False for all of those, and additionally when `site.enable_downvotes` is False, when `community.local_only and not user.is_local()`, when the user's attitude is below zero or reputation below -10, and on a `downvote_accept_mode` mismatch.

**`site.enable_downvotes = False` is the cheapest lever for the downvote gates** — it is one field on the `Site` row `_seed()` already creates, and it does not disturb upvotes. `user.bot = True` is the cheapest lever for the upvote gate, and it blocks both.

Do **not** reach for `community.local_only` here. `_seed()` uses `community.private` as the federation lever precisely so that `local_only` stays False; setting it would disable downvotes as a side effect and make a gate test pass for the wrong reason.

- [ ] **Step 3: Write the failing tests**

Append:

```python
def test_an_api_upvote_from_a_bot_returns_early_without_voting(db_session, app, redis_double):
    """`:35`'s true arm and `:36`'s early return.

    `can_upvote` rejects a bot, so `:36` returns the user id having voted for
    nothing. Asserts no PostVote row, because `:36` and `:63` return the same
    value and the return alone cannot tell them apart.
    """
    s = _seed()
    s.voter.bot = True
    db.session.commit()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                               auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_an_api_downvote_returns_early_when_downvotes_are_disabled(db_session, app, redis_double):
    """`:35`'s false arm, `:37`'s true arm and `:38`'s early return.

    Catches a regression collapsing `:37` into `:35`, which would let a
    downvote through while downvotes are off site-wide.
    """
    s = _seed()
    s.site.enable_downvotes = False
    db.session.commit()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'downvote', True, None, SRC_API,
                               auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_an_api_upvote_passes_both_gates_and_records_the_vote(db_session, app, redis_double):
    """`:35`'s and `:37`'s false arms together, and `:63`'s return.

    The false-arm counterpart of the two tests above. Catches a regression
    inverting either gate, which would reject a permitted vote.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                               auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 1


def test_a_web_upvote_from_a_bot_renders_empty_voting_buttons(db_session, app, redis_double):
    """`:43`'s FIRST conjunction taken true -- upvote and not can_upvote.

    `:43-44` is one arc pair to coverage.py, so this and the three tests below
    are the only way to distinguish its parts, and Task 11's mutation pass is
    the only instrument that proves they are distinguished. Catches a regression
    dropping the first disjunct.
    """
    s = _seed()
    s.voter.bot = True
    db.session.commit()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert isinstance(result, str)
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_a_web_downvote_is_refused_when_downvotes_are_disabled(db_session, app, redis_double):
    """`:44`'s SECOND conjunction taken true -- downvote and not can_downvote,
    with the first disjunct false because the direction is not 'upvote'.

    Catches a regression dropping the second disjunct, which would let the
    downvote through to `:58`.
    """
    s = _seed()
    s.site.enable_downvotes = False
    db.session.commit()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'downvote', True, None, SRC_WEB)

    assert isinstance(result, str)
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_a_web_upvote_is_allowed_when_only_downvotes_are_disabled(db_session, app, redis_double):
    """Both disjuncts false with `can_downvote` False -- the input that proves
    `:44`'s direction check is load-bearing.

    Without this test, a regression replacing `:44`'s
    `vote_direction == 'downvote'` with True would still pass everything above.
    """
    s = _seed()
    s.site.enable_downvotes = False
    db.session.commit()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert isinstance(result, str)
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 1


def test_a_web_downvote_is_allowed_when_nothing_blocks_it(db_session, app, redis_double):
    """Both disjuncts false with both helpers True -- the fourth input.

    Completes the four-way exercise of `:43-44`. Catches a regression replacing
    `:43`'s `vote_direction == 'upvote'` with True.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'downvote', True, None, SRC_WEB)

    assert isinstance(result, str)
    vote = db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).one()
    assert vote.effect < 0


def test_the_masonry_template_is_chosen_when_a_style_is_requested(db_session, app, redis_double):
    """`:45`'s true arm, the refusal path's masonry template.

    `:45` and `:72` are two separate ternaries with identical text; this pins
    `:45`'s. Catches a regression hardcoding either template name.
    """
    s = _seed()
    s.voter.bot = True
    db.session.commit()

    with _web_ctx(app, s.voter, query_string='style=masonry'):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert isinstance(result, str)
```

- [ ] **Step 4: Run them**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 31 passed.

If the two voting-button templates fail to render, apply Task 1's Probe B finding. If Probe B did not reach them, record the verbatim template error and arrange the minimum they require — and say what that was, because Group B's moderator verbs render nothing and Group A is the only round that pays this cost.

- [ ] **Step 5: Assert the two style templates differ**

The masonry test above asserts only that a string came back. Strengthen it: capture the non-masonry render from `test_a_web_upvote_from_a_bot_renders_empty_voting_buttons` and assert the two strings are not equal, either by comparing within one test or by asserting a marker unique to one template. State in the report which you did. **A test that cannot tell the two templates apart does not pin `:45`.**

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: cover vote_for_post's source fork and permission gates`

---

## Task 6: `vote_for_post`, the ban check through to the return arms

**Files:**
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `_web_ctx(app, user, query_string='')`, `_bearer(user)`.

Target: `:50`'s ban compound with each conjunct separately, `:55`'s quota boundary in both directions, `:58`'s `undo`, `:62`'s return fork, and `:67`/`:69`'s `undo is None` arms including the not-None case.

- [ ] **Step 1: Learn how the quota is raised**

`votes_cast_today(user_id)` is in `app/models.py`, not `app/utils.py`. Confirm:

```bash
grep -n "def votes_cast_today" -A 6 app/models.py
```

It does `from app import redis_client` inside the function body and reads the key `votes_cast_{date.today()}_{user_id}`, returning 0 when the key is absent. So the quota is raised by writing that key into fakeredis — no database work at all. `current_app.config['VOTE_QUOTA']` is the threshold.

This is a third function-body redis import, alongside `mark_post_read:1126` and `delete_post:765`. Task 1's Probe A answered whether `redis_double` reaches that shape; the same answer applies here.

- [ ] **Step 2: Write the failing tests**

Append:

```python
def _set_votes_cast_today(app, user_id, count):
    """Write the redis key `votes_cast_today` reads.

    app/models.py's `votes_cast_today` does `from app import redis_client`
    inside its own body and reads `votes_cast_{date.today()}_{user_id}`,
    returning 0 when the key is absent. Raising the quota therefore needs no
    database rows at all.
    """
    from datetime import date
    from app import redis_client
    redis_client.set(f'votes_cast_{date.today()}_{user_id}', str(count))


def test_a_banned_user_is_aborted_with_403(db_session, app, redis_double):
    """`:50`'s FIRST conjunct -- `user.banned` -- and `:51`'s abort.

    `:50` is `user.banned or user_ip_banned()`, one arc pair. This takes the
    first disjunct alone. Asserts the status code rather than merely that
    something raised, and asserts no vote was recorded, so an abort reached
    through an unrelated crash cannot satisfy it.
    """
    from werkzeug.exceptions import Forbidden

    s = _seed()
    s.voter.banned = True
    db.session.commit()

    with _web_ctx(app, s.voter):
        with pytest.raises(Forbidden) as excinfo:
            vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                          auth=_bearer(s.voter))

    assert excinfo.value.code == 403
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_a_vote_over_the_daily_quota_is_aborted_with_429(db_session, app, redis_double):
    """`:55`'s true arm and `:56`'s abort.

    Sets the counter one above VOTE_QUOTA, the tight side of `>`. Asserts 429
    specifically, because `:51` aborts 403 on the same function and a bare
    raises() cannot tell them apart.
    """
    from werkzeug.exceptions import TooManyRequests

    s = _seed()
    _set_votes_cast_today(app, s.voter.id, app.config['VOTE_QUOTA'] + 1)

    with _web_ctx(app, s.voter):
        with pytest.raises(TooManyRequests) as excinfo:
            vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                          auth=_bearer(s.voter))

    assert excinfo.value.code == 429
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_a_vote_exactly_at_the_daily_quota_is_allowed(db_session, app, redis_double):
    """`:55`'s false arm at the boundary itself.

    `:55` is `>`, so a count EQUAL to VOTE_QUOTA passes. This is the direction
    sub-project 32's mutation pass failed to probe, and the reason this plan
    asks for both. Catches a regression changing `>` to `>=`.
    """
    s = _seed()
    _set_votes_cast_today(app, s.voter.id, app.config['VOTE_QUOTA'])

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                               auth=_bearer(s.voter))

    assert result == s.voter.id
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 1


def test_a_first_web_upvote_reports_the_post_as_recently_upvoted(db_session, app, redis_double):
    """`:67`'s true arm and `:68`'s assignment, plus `:62`'s false arm.

    `Post.vote` returns None for a fresh vote, so `undo is None` holds and the
    post id lands in recently_upvoted. Catches a regression inverting `:67`.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert isinstance(result, str)
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 1


def test_a_first_web_downvote_reports_the_post_as_recently_downvoted(db_session, app, redis_double):
    """`:67`'s false arm, `:69`'s true arm and `:70`'s assignment.

    Catches a regression collapsing `:69` into `:67`, which would report a
    downvote as an upvote to the template.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'downvote', True, None, SRC_WEB)

    assert isinstance(result, str)
    vote = db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).one()
    assert vote.effect < 0


def test_repeating_a_web_upvote_undoes_it_and_reports_neither_list(db_session, app, redis_double):
    """`:67`'s and `:69`'s false arms together, via a non-None `undo`.

    `Post.vote` returns 'Like' when it removes an existing upvote
    (app/models.py, inside `vote`), so `undo is None` is False on both
    conditionals and both lists stay empty. This is the only input that takes
    both false arms. Catches a regression dropping the `undo is None` clause
    from either, which would report an undone vote as a live one.
    """
    s = _seed()
    with _web_ctx(app, s.voter):
        vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    with _web_ctx(app, s.voter):
        result = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert isinstance(result, str)
    assert db.session.query(PostVote).filter_by(
        user_id=s.voter.id, post_id=s.post.id).count() == 0


def test_the_masonry_template_is_chosen_on_the_success_path_too(db_session, app, redis_double):
    """`:72`'s true arm, distinct from `:45`'s.

    Coverage records two separate ternaries at `:45` and `:72`; Task 5 pinned
    `:45`'s on the refusal path and this pins `:72`'s on the success path.
    Catches a regression hardcoding either template name at `:72`.
    """
    s = _seed()

    with _web_ctx(app, s.voter, query_string='style=masonry'):
        masonry = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    with _web_ctx(app, s.voter):
        plain = vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    assert masonry != plain


def test_voting_marks_the_post_read(db_session, app, redis_double):
    """`:53`'s `mark_post_read` call on the success path.

    Task 7 moves this call below the quota check; this test pins that it still
    happens on the path where the vote succeeds, so that move cannot silently
    delete it.
    """
    s = _seed()

    with _web_ctx(app, s.voter):
        vote_for_post(s.post.id, 'upvote', True, None, SRC_WEB)

    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert {row.read_post_id for row in rows} == {s.post.id}
```

- [ ] **Step 3: Run them**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 39 passed.

If `werkzeug.exceptions.TooManyRequests` is not what `abort(429)` raises in this Flask version, use `werkzeug.exceptions.HTTPException` and assert `excinfo.value.code == 429`. **Do not fall back to a bare `pytest.raises(Exception)`** — the status code is the whole assertion.

- [ ] **Step 4: Confirm `:50`'s second conjunct is deliberately not covered here**

`user_ip_banned()` is the second disjunct of `:50`. Covering it needs a banned IP row matching the test request's remote address, which is more machinery than this arc is worth. **Coverage does not need it** — `:50`'s arc pair is already taken by the banned-user test and by every passing test. State in the report that the second disjunct is unpinned by test and will be probed by Task 11's mutation pass instead, so that the gap is a recorded decision rather than an oversight.

- [ ] **Step 5: Commit**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: cover vote_for_post's ban check, quota and return arms`

---

## Task 7: PC3 — stop an over-quota vote from marking the post read

**Files:**
- Modify: `app/shared/post.py:53` and `:55`
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `_web_ctx`, `_bearer`, `_set_votes_cast_today(app, user_id, count)` from Task 6.

`vote_for_post:53` calls `mark_post_read([post.id], True, user.id)` and `:55` then checks the quota. A user over quota therefore has the post written to `read_posts` and `last_seen` bumped — `mark_post_read:1118-1120` writes the row and `:1128-1130` bumps `last_seen` and commits — and only then receives a 429. The side effect survives the rejection.

**The ordering against `:50`'s ban check is already correct and must stay that way**: a banned user aborts before either.

- [ ] **Step 1: Re-derive the lines**

```bash
awk 'NR>=49 && NR<=61 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
wc -l app/shared/post.py
```

Record the current `wc -l`. This change moves one line and adds none, so the file length must be **unchanged** afterwards.

- [ ] **Step 2: Write the failing observation**

Append:

```python
def test_an_over_quota_vote_does_not_mark_the_post_read(db_session, app, redis_double):
    """PC3: `:53`'s mark_post_read runs before `:55`'s quota check.

    A user over quota has the post written to read_posts and last_seen bumped,
    and only then receives a 429 -- the side effect survives the rejection.
    This test fails against the tree as it stands and passes once the call moves
    below the check.

    Asserts the 429 as well as the empty table, so a change that stopped the
    abort entirely could not make it pass.
    """
    from werkzeug.exceptions import TooManyRequests

    s = _seed()
    _set_votes_cast_today(app, s.voter.id, app.config['VOTE_QUOTA'] + 1)

    with _web_ctx(app, s.voter):
        with pytest.raises(TooManyRequests) as excinfo:
            vote_for_post(s.post.id, 'upvote', True, None, SRC_API,
                          auth=_bearer(s.voter))

    assert excinfo.value.code == 429
    rows = db.session.execute(
        read_posts.select().where(read_posts.c.user_id == s.voter.id)).fetchall()
    assert rows == []
```

- [ ] **Step 3: Run it and record the failure verbatim**

Run: `./run_tests.sh tests/test_shared_post_interactions.py::test_an_over_quota_vote_does_not_mark_the_post_read -v`

Expected: FAIL. Paste the verbatim assertion output into the report. **A production change with no failing observation behind it is the same error as a test that cannot fail, one level up** — if this passes, stop and report that the defect does not exist as described.

- [ ] **Step 4: Move the call**

`app/shared/post.py:50-56` currently reads:

```python
    if user.banned or user_ip_banned():
        abort(403)

    mark_post_read([post.id], True, user.id)

    if votes_cast_today(user.id) > current_app.config['VOTE_QUOTA']:
        abort(429)
```

Change it to:

```python
    if user.banned or user_ip_banned():
        abort(403)

    if votes_cast_today(user.id) > current_app.config['VOTE_QUOTA']:
        abort(429)

    mark_post_read([post.id], True, user.id)
```

- [ ] **Step 5: Run the file**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 40 passed. `test_voting_marks_the_post_read` from Task 6 must still pass — it exists precisely so this move cannot silently delete the call.

- [ ] **Step 6: Confirm the file length is unchanged**

```bash
wc -l app/shared/post.py
git diff --stat app/shared/post.py
```

Expected: the same line count recorded in Step 1, and a diff of 3 insertions and 3 deletions (or 4/4 with the blank line). If the count moved, the edit did something other than a move.

- [ ] **Step 7: Commit**

```bash
git add app/shared/post.py tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `fix: stop an over-quota vote from marking the post read`

The body must state the observed failure, not just the intent.

---

## Task 8: `vote_for_poll` against the tree as it stands

**Files:**
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `_web_ctx`, `_bearer`, `Poll`, `PollChoice`, `PollChoiceVote`.
- Produces: `_seed_poll(s, mode='single', choices=('a', 'b'))`, returning the `Poll`. Tasks 9 and 10 both consume it.

Target: `:1147`'s source fork, `:1152`'s ban compound, `:1155`'s `isinstance`, `:1159`'s mode fork, `:1160`'s length check, `:1163`'s `has_voted` fork, `:1168`'s API raise, and `:1171`'s loop.

**This task covers the function as it is today.** Tasks 9 and 10 change it; their new arms are their own tasks' work.

- [ ] **Step 1: Re-derive the line numbers**

```bash
awk 'NR>=1146 && NR<=1174 {printf "%d\t%s\n",NR,$0}' app/shared/post.py
```

Confirm `:1155` is `if isinstance(votes, int):`, `:1158` the `Poll.query.get_or_404`, `:1159` `if poll.mode == 'single':`, `:1163` `if not poll.has_voted(user.id):`, `:1171` `for choice_id in votes:`.

- [ ] **Step 2: Know the model shape before writing the fixture**

`Poll.post_id` is the primary key (`app/models.py:3781`) — a poll IS its post, there is no separate poll id. `PollChoice` has its own autoincrement `id` at `app/models.py:3820` and a `post_id` foreign key at `:3821`. `PollChoiceVote` carries `choice_id`, `user_id` and `post_id`.

`Poll.vote_for_choice(choice_id, user_id)` (`app/models.py:3794-3803`) filters `PollChoiceVote` on `user_id` and `choice_id` only — **it ignores `post_id`** — and on a miss constructs `PollChoiceVote(choice_id=choice_id, user_id=user_id, post_id=self.post_id)`, increments `PollChoice.num_votes`, sets `latest_vote` and commits. On a hit it does nothing at all and returns None either way.

`Poll.has_voted(user_id)` (`app/models.py:3789-3792`) filters on `user_id` and `post_id`, so it is poll-scoped where `vote_for_choice`'s own check is not. That asymmetry is what Tasks 9 and 10 are about.

- [ ] **Step 3: Write the poll fixture and the tests**

Append:

```python
def _seed_poll(s, mode='single', choices=('a', 'b')):
    """A Poll on `s.post` with the named choices.

    `Poll.post_id` is the primary key (app/models.py:3781) -- a poll IS its
    post, so this takes the seeded post rather than making a new id. Returns the
    Poll; read its choices through `poll.choices` or query PollChoice by
    post_id.
    """
    from datetime import timedelta
    from app.models import utcnow

    poll = Poll(post_id=s.post.id, mode=mode, local_only=False,
                end_poll=utcnow() + timedelta(days=1))
    db.session.add(poll)
    for order, text in enumerate(choices):
        db.session.add(PollChoice(post_id=s.post.id, choice_text=text,
                                  sort_order=order, num_votes=0))
    db.session.commit()
    return poll


def test_a_single_mode_api_vote_records_one_choice(db_session, app, redis_double):
    """`:1147`'s true arm, `:1155`'s true arm, `:1159`'s true arm, `:1163`'s
    true arm, `:1164`'s vote and `:1165`'s federation call.

    The API caller passes a bare int (app/api/alpha/utils/post.py:1793 hands
    over `data['choice_id']` unwrapped), so `:1155` wraps it. Catches a
    regression dropping `:1155`, which would make `len(votes)` fail on an int.
    """
    s = _seed()
    poll = _seed_poll(s, mode='single')
    choice = db.session.query(PollChoice).filter_by(post_id=s.post.id).order_by(
        PollChoice.sort_order).first()

    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, choice.id, SRC_API, auth=_bearer(s.voter))

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == {choice.id}
    db.session.refresh(choice)
    assert choice.num_votes == 1


def test_a_single_mode_web_vote_accepts_a_list(db_session, app, redis_double):
    """`:1147`'s false arm and `:1155`'s false arm.

    app/post/routes.py:642 passes an int for single mode, but the false arm of
    `:1155` still needs an input that is already a list. Catches a regression
    making `:1155` unconditional, which would wrap a list into a nested list.
    """
    s = _seed()
    poll = _seed_poll(s, mode='single')
    choice = db.session.query(PollChoice).filter_by(post_id=s.post.id).order_by(
        PollChoice.sort_order).first()

    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [choice.id], SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == {choice.id}


def test_a_banned_user_cannot_vote_in_a_poll(db_session, app, redis_double):
    """`:1152`'s first conjunct and `:1153`'s abort.

    Asserts 403 specifically and asserts no vote row, so an abort reached
    through an unrelated crash cannot satisfy it.
    """
    from werkzeug.exceptions import Forbidden

    s = _seed()
    poll = _seed_poll(s, mode='single')
    choice = db.session.query(PollChoice).filter_by(post_id=s.post.id).first()
    s.voter.banned = True
    db.session.commit()

    with _web_ctx(app, s.voter):
        with pytest.raises(Forbidden) as excinfo:
            vote_for_poll(s.post.id, choice.id, SRC_API, auth=_bearer(s.voter))

    assert excinfo.value.code == 403
    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0


def test_a_second_single_mode_vote_raises_through_the_api(db_session, app, redis_double):
    """`:1163`'s false arm and `:1169`'s raise, through `:1168`'s true arm.

    `has_voted` is poll-scoped (app/models.py:3789-3792), so a second vote in
    the same poll is rejected even for a different choice. Asserts the vote
    count stayed at one.
    """
    s = _seed()
    poll = _seed_poll(s, mode='single')
    first, second = db.session.query(PollChoice).filter_by(
        post_id=s.post.id).order_by(PollChoice.sort_order).all()
    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, first.id, SRC_API, auth=_bearer(s.voter))

    with _web_ctx(app, s.voter):
        with pytest.raises(Exception, match='already voted'):
            vote_for_poll(s.post.id, second.id, SRC_API, auth=_bearer(s.voter))

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 1


def test_a_second_single_mode_vote_is_silently_ignored_on_the_web(db_session, app, redis_double):
    """`:1168`'s false arm.

    The web arm must not raise -- app/post/routes.py:643 has no handler and
    flashes 'Vote has been cast.' unconditionally afterwards. Catches a
    regression hoisting `:1169` out of `:1168`.
    """
    s = _seed()
    poll = _seed_poll(s, mode='single')
    first, second = db.session.query(PollChoice).filter_by(
        post_id=s.post.id).order_by(PollChoice.sort_order).all()
    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [first.id], SRC_WEB)

    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [second.id], SRC_WEB)

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 1


def test_too_many_choices_in_single_mode_raises_through_the_api(db_session, app, redis_double):
    """`:1160`'s true arm and `:1162`'s raise, through `:1161`'s true arm.

    Asserts no vote was recorded, because `:1162` raises before `:1163` and a
    regression that raised after voting would otherwise look identical.
    """
    s = _seed()
    poll = _seed_poll(s, mode='single')
    first, second = db.session.query(PollChoice).filter_by(
        post_id=s.post.id).order_by(PollChoice.sort_order).all()

    with _web_ctx(app, s.voter):
        with pytest.raises(Exception, match='single'):
            vote_for_poll(s.post.id, [first.id, second.id], SRC_API,
                          auth=_bearer(s.voter))

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0


def test_too_many_choices_in_single_mode_falls_through_on_the_web(db_session, app, redis_double):
    """`:1161`'s false arm -- the fall-through this round registers as PC4.

    `:1160`'s length check raises only for SRC_API. The web arm falls through to
    `:1164`, which votes for `votes[0]` and silently discards the rest. That is
    unreachable from app/post/routes.py:642, which normalizes single mode to one
    int before calling, so it is registered rather than fixed -- and pinned here
    so the fall-through cannot change unnoticed.
    """
    s = _seed()
    poll = _seed_poll(s, mode='single')
    first, second = db.session.query(PollChoice).filter_by(
        post_id=s.post.id).order_by(PollChoice.sort_order).all()

    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [first.id, second.id], SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == {first.id}


def test_multiple_mode_records_every_choice(db_session, app, redis_double):
    """`:1159`'s false arm and `:1171`'s loop over several choices.

    Catches a regression sending a multiple-mode poll down the single-mode
    branch, which would record only the first choice.
    """
    s = _seed()
    poll = _seed_poll(s, mode='multiple', choices=('a', 'b', 'c'))
    all_choices = db.session.query(PollChoice).filter_by(post_id=s.post.id).all()
    ids = {c.id for c in all_choices}

    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, sorted(ids), SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == ids


def test_multiple_mode_with_no_choices_records_nothing(db_session, app, redis_double):
    """`:1171`'s zero-iteration exit arc.

    app/post/routes.py:642 uses `request.form.getlist` for multiple mode, which
    returns [] when the voter submits nothing, so this input is reachable.
    Catches a regression giving the loop a default choice.
    """
    s = _seed()
    poll = _seed_poll(s, mode='multiple')

    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, [], SRC_WEB)

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0
```

- [ ] **Step 4: Run them**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 49 passed.

If `Poll.query.get_or_404` raises outside a request context, note that `_web_ctx` supplies one for every test here — that is one more reason the harness is what it is.

- [ ] **Step 5: Report the `has_voted` versus `vote_for_choice` asymmetry**

State explicitly in the report that `has_voted` filters on `post_id` while `vote_for_choice`'s own duplicate check does not, and that this asymmetry is what Tasks 9 and 10 act on. The controller needs this confirmed by a running test rather than by reading.

- [ ] **Step 6: Commit**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: cover vote_for_poll's single and multiple mode branches`

---

## Task 9: PC1 — reject a choice that belongs to another poll

**Files:**
- Modify: `app/shared/post.py`, inside `vote_for_poll` only
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `_seed_poll(s, mode=..., choices=...)`, `_web_ctx`, `_bearer`.

`app/api/alpha/utils/post.py:1791-1797` reads `choice_id` from the request body and passes it to `vote_for_poll` unexamined. Neither `:1164` nor `:1172` checks that the choice belongs to `post_id`. `Poll.vote_for_choice` then writes `PollChoiceVote(choice_id=choice_id, user_id=user_id, post_id=self.post_id)` — `post_id` from the *target* poll — and increments `num_votes` on the *foreign* choice.

A nonexistent `choice_id` fails differently and worse: `:1166` evaluates `PollChoice.query.get(votes[0]).choice_text`, dereferencing `None`.

**Decided semantics, from the spec:** filter the requested choices to those belonging to this poll, before any voting. A rejected choice **raises for `SRC_API` and is skipped silently for `SRC_WEB`** — the idiom `:1160-1162` and `:1168-1169` already use twice. This avoids giving `app/post/routes.py:635-646` a failure mode it has never had.

- [ ] **Step 1: Write the two failing observations**

Append:

```python
def test_an_api_vote_for_another_polls_choice_is_rejected(db_session, app, redis_double):
    """PC1: `vote_for_poll` never checks that a choice belongs to the poll.

    app/api/alpha/utils/post.py:1793 hands `data['choice_id']` straight through.
    Against the tree as it stands this records a PollChoiceVote whose post_id is
    the target poll and whose choice_id belongs to another, and increments the
    FOREIGN choice's num_votes. Fails today, passes once the membership filter
    lands.
    """
    s = _seed()
    _seed_poll(s, mode='single')
    other_post = make_post(s.community, s.author, 'https://local.example/p/other')
    from datetime import timedelta
    from app.models import utcnow
    other_poll = Poll(post_id=other_post.id, mode='single', local_only=False,
                      end_poll=utcnow() + timedelta(days=1))
    db.session.add(other_poll)
    db.session.add(PollChoice(post_id=other_post.id, choice_text='foreign',
                              sort_order=0, num_votes=0))
    db.session.commit()
    foreign = db.session.query(PollChoice).filter_by(post_id=other_post.id).one()

    with _web_ctx(app, s.voter):
        with pytest.raises(Exception):
            vote_for_poll(s.post.id, foreign.id, SRC_API, auth=_bearer(s.voter))

    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0
    db.session.refresh(foreign)
    assert foreign.num_votes == 0


def test_a_nonexistent_choice_id_does_not_dereference_none(db_session, app, redis_double):
    """PC1's second effect: `:1166` reads `PollChoice.query.get(votes[0]).choice_text`.

    A choice id matching no row makes that an AttributeError on None. The
    membership filter closes it because a nonexistent choice is not a member.
    Fails today with AttributeError, passes once the filter lands.
    """
    s = _seed()
    _seed_poll(s, mode='single')
    highest = db.session.query(PollChoice).order_by(PollChoice.id.desc()).first()

    with _web_ctx(app, s.voter):
        with pytest.raises(Exception) as excinfo:
            vote_for_poll(s.post.id, highest.id + 1000, SRC_API,
                          auth=_bearer(s.voter))

    assert 'NoneType' not in str(excinfo.value)
    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 0
```

- [ ] **Step 2: Run them and record both failures verbatim**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -k "another_polls_choice or nonexistent_choice" -v`

Expected: both FAIL. Paste both verbatim into the report, including the `AttributeError: 'NoneType' object has no attribute 'choice_text'` from the second. If either passes, stop and report — the defect is not what this plan describes.

- [ ] **Step 3: Add the membership filter**

Edit `vote_for_poll` in `app/shared/post.py`. After `:1158`'s `poll = Poll.query.get_or_404(post_id)` and before `:1159`'s mode fork, insert:

```python
    poll_choice_ids = {row.id for row in
                       db.session.query(PollChoice).filter_by(post_id=post_id)}
    foreign = [choice_id for choice_id in votes if int(choice_id) not in poll_choice_ids]
    if foreign:
        if src == SRC_API:
            raise Exception("Choice does not belong to this poll.")
        votes = [choice_id for choice_id in votes if int(choice_id) in poll_choice_ids]
```

`int(choice_id)` matches `:1172`'s existing `poll.vote_for_choice(int(choice_id), user.id)` — the multiple-mode arm already coerces, because `request.form.getlist` yields strings.

- [ ] **Step 4: Run the whole file**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 51 passed. Every Task 8 test must still pass — in particular `test_multiple_mode_records_every_choice`, which is the demonstration that the filter does not reject legitimate choices.

- [ ] **Step 5: Demonstrate that only the foreign choice is rejected**

The spec requires this explicitly, because a wrong filter rejects legitimate choices and that is worse than the defect. Append:

```python
def test_the_membership_filter_keeps_every_legitimate_choice(db_session, app, redis_double):
    """PC1's safety demonstration, required by the spec.

    A multiple-mode vote naming two of this poll's choices and one foreign
    choice must record both legitimate votes and reject only the foreign one.
    Catches a filter that rejects the whole request, or one that matches on the
    wrong column and drops everything.
    """
    s = _seed()
    _seed_poll(s, mode='multiple', choices=('a', 'b'))
    other_post = make_post(s.community, s.author, 'https://local.example/p/other')
    from datetime import timedelta
    from app.models import utcnow
    db.session.add(Poll(post_id=other_post.id, mode='single', local_only=False,
                        end_poll=utcnow() + timedelta(days=1)))
    db.session.add(PollChoice(post_id=other_post.id, choice_text='foreign',
                              sort_order=0, num_votes=0))
    db.session.commit()
    mine = {c.id for c in db.session.query(PollChoice).filter_by(post_id=s.post.id)}
    foreign = db.session.query(PollChoice).filter_by(post_id=other_post.id).one()

    with _web_ctx(app, s.voter):
        vote_for_poll(s.post.id, sorted(mine) + [foreign.id], SRC_WEB)

    votes = db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).all()
    assert {v.choice_id for v in votes} == mine
    db.session.refresh(foreign)
    assert foreign.num_votes == 0
```

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 52 passed.

- [ ] **Step 6: Record the file length**

```bash
wc -l app/shared/post.py
```

The insertion adds 7 lines to the count recorded in Task 7 Step 6. State both numbers in the report. **Do not predict the delta from the diff's insertion count** — count the lines the edit actually adds and check against `wc -l`, because this campaign has had that arithmetic wrong three times.

- [ ] **Step 7: Commit**

```bash
git add app/shared/post.py tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `fix: reject a poll vote for a choice belonging to another poll`

---

## Task 10: PC2 — federate only votes that were actually recorded

**Files:**
- Modify: `app/shared/post.py`, inside `vote_for_poll`'s multiple-mode loop only
- Modify: `tests/test_shared_post_interactions.py`

**Interfaces:**
- Consumes: `_seed()`, `_seed_poll`, `_web_ctx`, `_bearer`.

`Poll.vote_for_choice` is a silent no-op when `PollChoiceVote` already holds a row for that `(user_id, choice_id)` pair — `app/models.py:3797`'s `if not existing_vote:` guards the entire body. But `vote_for_poll:1173` calls `task_selector('vote_for_poll', ...)` unconditionally inside the `:1171` loop. Re-submitting the same choices to a multiple-mode poll changes nothing locally and federates one phantom vote per choice.

The single-mode arm does not have this defect: `:1163`'s `if not poll.has_voted(user.id):` guards both the vote and the `task_selector` call together.

- [ ] **Step 1: Enumerate `vote_for_choice`'s callers before choosing an approach**

The spec permits two resolutions and requires the enumeration first:

```bash
grep -rn "vote_for_choice" app/ tests/ --include=*.py
```

If the only callers are `vote_for_poll:1164` and `:1172`, changing `Poll.vote_for_choice` to return a bool is contained and is the cleaner fix. **If there are other callers, take the call-site resolution instead** — determine at the call site whether a `PollChoiceVote` row already exists, and skip the federation call when it does. Record the enumeration's output and which resolution you took, and why.

- [ ] **Step 2: Write the failing observation**

Append:

```python
def test_resubmitting_multiple_mode_choices_does_not_federate_again(db_session, app, redis_double):
    """PC2: `:1173`'s task_selector fires even when `vote_for_choice` no-ops.

    `Poll.vote_for_choice` guards its whole body with `if not existing_vote:`
    (app/models.py:3797), so a repeat submission records nothing -- but `:1173`
    federates one phantom vote per choice regardless, and remote instances
    increment totals the origin does not have.

    Counts task_selector calls rather than inspecting network traffic, because
    task_selector runs synchronously under eager Celery and the poll task
    returns at its own first guard with `community.private` set.
    """
    calls = []
    s = _seed()
    _seed_poll(s, mode='multiple', choices=('a', 'b'))
    ids = sorted(c.id for c in
                 db.session.query(PollChoice).filter_by(post_id=s.post.id))

    import app.shared.post as post_module
    original = post_module.task_selector

    def counting_task_selector(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = counting_task_selector
    try:
        with _web_ctx(app, s.voter):
            vote_for_poll(s.post.id, ids, SRC_WEB)
        first_round = len(calls)

        with _web_ctx(app, s.voter):
            vote_for_poll(s.post.id, ids, SRC_WEB)
    finally:
        post_module.task_selector = original

    assert first_round == 2
    assert len(calls) == 2, \
        'the repeat submission federated votes that were never recorded'
    assert db.session.query(PollChoiceVote).filter_by(user_id=s.voter.id).count() == 2
```

- [ ] **Step 3: Run it and record the failure verbatim**

Run: `./run_tests.sh tests/test_shared_post_interactions.py::test_resubmitting_multiple_mode_choices_does_not_federate_again -v`

Expected: FAIL, with `len(calls) == 4` against the expected 2. Paste the verbatim output.

**Note the monkeypatch restores in a `finally`.** `task_selector` is a module-level name that other tests in the same session import; leaving it patched would corrupt every test that ran afterwards. This is the same shared-object hazard `tests/README.md` fact 201 records for `cache.delete_memoized`.

- [ ] **Step 4: Apply the resolution chosen in Step 1**

**If `vote_for_choice`'s only callers are in `vote_for_poll`**, change `app/models.py:3794-3803` to return True when it created a vote and False when it did not, then guard the federation call:

```python
        for choice_id in votes:
            if poll.vote_for_choice(int(choice_id), user.id):
                task_selector('vote_for_poll', post_id=post_id, user_id=user.id,
                              choice_text=PollChoice.query.get(int(choice_id)).choice_text)
```

**If there are other callers**, leave `vote_for_choice` alone and determine it at the call site:

```python
        for choice_id in votes:
            already_voted = db.session.query(PollChoiceVote).filter(
                PollChoiceVote.user_id == user.id,
                PollChoiceVote.choice_id == int(choice_id)).first() is not None
            poll.vote_for_choice(int(choice_id), user.id)
            if not already_voted:
                task_selector('vote_for_poll', post_id=post_id, user_id=user.id,
                              choice_text=PollChoice.query.get(int(choice_id)).choice_text)
```

The call-site version's filter matches `vote_for_choice`'s own — `user_id` and `choice_id`, not `post_id` — deliberately, so the two cannot disagree about what counts as a duplicate.

- [ ] **Step 5: Run the whole file**

Run: `./run_tests.sh tests/test_shared_post_interactions.py -v`

Expected: 53 passed.

- [ ] **Step 6: Run the poll task's own test file if one exists**

```bash
ls tests/ | grep -i "likes\|poll"
```

If a test file covers `app/shared/tasks/likes.py` or `Poll.vote_for_choice`, run it. If you changed `vote_for_choice`'s return, run it regardless of what it is named:

Run: `./run_tests.sh tests/ -k "poll or vote_for_choice" -v`

Expected: no new failures. Paste the count.

- [ ] **Step 7: Record the file lengths**

```bash
wc -l app/shared/post.py app/models.py
```

State both against the numbers recorded in Task 9.

- [ ] **Step 8: Commit**

```bash
git add app/shared/post.py app/models.py tests/test_shared_post_interactions.py
git commit -F <message-file>
```

(Drop `app/models.py` from the `git add` if you took the call-site resolution.)

Subject: `fix: federate a poll vote only when it was actually recorded`

---

## Task 11: Mutation pass

**Files:**
- Modify: none permanently. `app/shared/post.py` is mutated and restored, one site at a time.

**Interfaces:**
- Consumes: the complete `tests/test_shared_post_interactions.py`.

**Mutation is not redundant with coverage at any level.** Sub-project 32's pass found six holes at zero missing statements; sub-project 33's found four more at 100%, every one inside a construct coverage.py records as a single arc pair. This round has three such constructs — `:43-44`, `:50`, and the compound the membership filter introduced.

- [ ] **Step 1: Record the baseline**

```bash
git diff --stat -- app/
wc -l app/shared/post.py
```

Expected: empty diff. Record the line count; every restore must return to it.

- [ ] **Step 2: Build the site table**

Every site below gets a row. **Enumerate them all before mutating any** — sub-project 32's pass named a site in its own prose and then never mutated it, and the final review found the hole.

| # | Site | Mutation |
|---|---|---|
| 1 | `:32` `if src == SRC_API:` | `SRC_WEB` |
| 2 | `:35` `vote_direction == 'upvote'` | `'downvote'` |
| 3 | `:35` `not can_upvote(...)` | drop the `not` |
| 4 | `:37` `vote_direction == 'downvote'` | `'upvote'` |
| 5 | `:37` `not can_downvote(...)` | drop the `not` |
| 6 | `:43` `vote_direction == 'upvote'` | `True` |
| 7 | `:43` `not can_upvote(...)` | drop the `not` |
| 8 | `:44` `vote_direction == 'downvote'` | `True` |
| 9 | `:44` `not can_downvote(...)` | drop the `not` |
| 10 | `:45` `== ''` | `!= ''` |
| 11 | `:50` `user.banned` | `False` |
| 12 | `:50` `user_ip_banned()` | `False` |
| 13 | `:55` `>` | `>=` |
| 14 | `:55` `>` | `<` |
| 15 | `:62` `if src == SRC_API:` | `SRC_WEB` |
| 16 | `:67` `vote_direction == 'upvote'` | `'downvote'` |
| 17 | `:67` `undo is None` | `undo is not None` |
| 18 | `:69` `undo is None` | `undo is not None` |
| 19 | `:72` `== ''` | `!= ''` |
| 20 | `:78` source ternary | swap the arms |
| 21 | `:83` `if not existing_bookmark:` | drop the `not` |
| 22 | `:88` `if src == SRC_API:` | `SRC_WEB` |
| 23 | `:101` `if existing_bookmark:` | add a `not` |
| 24 | `:106` `if src == SRC_API:` | `SRC_WEB` |
| 25 | `:119` `if src == SRC_WEB:` | `SRC_API` |
| 26 | `:120` ternary | swap `False` and `True` |
| 27 | `:124` `subscribe == False` | `subscribe != False` |
| 28 | `:125` `if existing_notification:` | add a `not` |
| 29 | `:136` `if existing_notification:` | add a `not` |
| 30 | `:1116` `read is True` | `read is not True` |
| 31 | `:1134` `isinstance(post, int)` | `isinstance(post, str)` |
| 32 | `:1137` `if not post.flair:` | drop the `not` |
| 33 | `:1147` `if src == SRC_API:` | `SRC_WEB` |
| 34 | `:1152` `user.banned` | `False` |
| 35 | `:1152` `user_ip_banned()` | `False` |
| 36 | `:1155` `isinstance(votes, int)` | `isinstance(votes, str)` |
| 37 | `:1159` `poll.mode == 'single'` | `!= 'single'` |
| 38 | `:1160` `len(votes) != 1` | `len(votes) != 2` |
| 39 | `:1161` `if src == SRC_API:` | `SRC_WEB` |
| 40 | `:1163` `if not poll.has_voted(...)` | drop the `not` |
| 41 | `:1168` `if src == SRC_API:` | `SRC_WEB` |
| 42 | the membership filter's `not in` | `in` |
| 43 | the membership filter's `if src == SRC_API:` | `SRC_WEB` |
| 44 | the membership filter's `filter_by(post_id=post_id)` | drop the filter |
| 45 | PC2's federation guard | invert it |

**Sites 13 and 14 are the same boundary mutated in both directions.** Sub-project 32's pass mutated a cutoff only the way the existing tests already caught, and its final review found the hole. Both directions, every boundary.

**Sites 3, 5, 7, 9, 11, 12, 34 and 35 are conjuncts of compounds coverage.py sees as single arc pairs.** They are the only instrument that can distinguish the four inputs Task 5 wrote and the two disjuncts of `:50` and `:1152`.

**Site 12 and site 35 are expected to survive**, because Task 6 Step 4 deliberately left `user_ip_banned()` unpinned by test. Record them as known-equivalent-by-decision rather than as holes, and say so — a survivor with a recorded reason is different from a survivor nobody noticed.

- [ ] **Step 3: For each site, run the loop**

For site N:

```bash
# 1. dry-run WITHOUT -i and READ the line it produces
sed -n '<LINE>p' app/shared/post.py
# 2. apply
sed -i '<LINE>s/<old>/<new>/' app/shared/post.py
# 3. confirm the edit landed and nothing else moved
git diff -- app/shared/post.py
# 4. run
./run_tests.sh tests/test_shared_post_interactions.py -x -q
# 5. restore
git checkout -- app/shared/post.py
# 6. assert clean
git diff -- app/; wc -l app/shared/post.py
```

**Read the dry-run line before applying.** A `sed` pattern that matches a different line than intended produces a mutation nobody is measuring.

**Restore before any point where you might stop and report.** A process that dies mid-probe cannot restore, and this campaign has had two do exactly that.

Paste every dry-run line and every failure into the report.

- [ ] **Step 4: Close every hole**

A site whose mutation the suite does not catch is a hole. For each, either add a test that catches it, or prove the mutation equivalent — with an argument, not an assertion. Sites 12 and 35 already have their argument from Step 2.

Re-run the full file after each new test.

- [ ] **Step 5: Final restore check**

```bash
git diff -- app/
wc -l app/shared/post.py
./run_tests.sh tests/test_shared_post_interactions.py -q
```

Expected: empty diff, the baseline line count, all passing.

- [ ] **Step 6: Commit any tests added**

```bash
git add tests/test_shared_post_interactions.py
git commit -F <message-file>
```

Subject: `test: close the holes the post interactions mutation pass found`

If the pass found no holes, skip the commit and say so.

---

## Task 12: Measure, raise the floor, and register

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `tests/factories.py:668` and `:682`, two stale line citations (Step 5, entry 13)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Measure the module**

```bash
./run_tests.sh tests/test_shared_post_interactions.py tests/test_shared_post_edit.py \
  --cov=app.shared.post --cov-branch --cov-report=json:/tmp/post34.json -q
echo "exit=$?"
```

**Dotted form, not a path.** A path form collects nothing, writes no JSON and exits 0.

- [ ] **Step 2: Read the number**

```bash
podman-compose -f compose.test.yaml exec -T test-runner \
  python -c "import json;d=json.load(open('/tmp/post34.json'));f=d['files']['app/shared/post.py'];print(f['summary'])"
```

> **CORRECTED POST-HOC (sub-project 34's final fix wave; Task 12 found it while
> measuring).** This line and the one in Step 7 below originally read
> `./run_tests.sh --exec python …`. **`run_tests.sh` has no `--exec` flag**:
> `run_tests.sh:85` is `exec $COMPOSE exec -T test-runner pytest "$@"`, so every
> argument goes to **pytest**, which rejects `--exec` with **exit 4** and runs
> nothing. The working form is the `podman-compose … exec -T test-runner python`
> invocation above, already documented at `tests/README.md:403-405`. The
> correction is recorded here rather than silently applied because four later
> sub-projects template their plan off this one and the history should stay
> honest about which command actually ran.

Read `percent_covered`, **not** `percent_statements_covered`. `tests/check_coverage_floors.py:75` reads the former.

Record: statements covered, statements missing, branches covered, branches partial, and `percent_covered`.

- [ ] **Step 3: Confirm Group A is closed**

From the same JSON, confirm no missing statement or missing arc falls in `:31-160` or `:1115-1174`. Missing lines elsewhere in the module are Groups B through E and are expected.

If any Group A line is still missing, that is the task's real finding — report it before touching the floor.

- [ ] **Step 4: Raise the floor**

```bash
grep -n "app/shared/post.py" coverage_floors.ini
```

Set it to the measured `percent_covered` rounded **down** to an integer. **This round does not close the module** — the floor lands well short of 100 and that is correct. Do not treat a sub-100 result as a failure to finish.

- [ ] **Step 5: Write the register entries**

Append to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, starting at **D392**, and update the "Next free number" line at the file's end.

Entries required, one number each:

1. The decomposition of `app/shared/post.py` into five groups, with each group's measured statements and arcs, the ordering, and why — so the next four rounds do not re-derive it.
2. The harness facts, this round's most reusable output: no `user=` escape in Group A; ~~both source arms need a request context because `user_ip_banned` resolves to a function reading `request`~~ — **CORRECTED POST-HOC (final fix wave): Probe D disproved this. Only the SRC_WEB arm needs a request context; `get_ip_address` (`app/__init__.py:68-77`) swallows the `RuntimeError`, so the SRC_API arm runs with no context at all. Registered as D394 and `tests/README.md` fact 206** — `community.private` is the federation lever and `local_only` is the trap because `can_downvote` reads it; whether `redis_double` reaches a function-body `from app import redis_client`; what the three templates required.
3. PC1, with the safety demonstration from Task 9 Step 5 recorded.
4. PC2, with the caller enumeration and which resolution was taken.
5. PC3.
6. PC4 — `:1160`'s length check is API-only and `:1161`'s WEB arm falls through to `:1164`'s `votes[0]`, unreachable today because `app/post/routes.py:642` normalizes single mode to one int. Registered, not fixed, because a change there would have no reachable failing observation.
7. `vote_for_poll` returns `None` on `SRC_API` where every sibling returns `user.id`; `app/api/alpha/utils/post.py:1797` ignores the return, so latent not live.
8. `mark_post_read:1116`'s `read is True` identity test, which routes a truthy non-`True` down the DELETE branch.
9. `vote_for_post:33`'s `.get()` against `:40`'s `get_or_404` — the API arm 500s at `:35`'s `post.community` where the WEB arm 404s. ~~Roughly nine sites of the same asymmetry across the module (`delete_post:757`, `restore_post:798`, `lock_post:933` among them), registered whole rather than fixed at one site.~~ **CORRECTED POST-HOC (final fix wave): the re-derivation refuted "roughly nine sites of the same asymmetry" twice over. `get_or_404` appears exactly TWICE in `app/shared/post.py` (`:40`, `:1158`) and `query(Post).get(` exactly ELEVEN times (`:33`, `:757`, `:767`, `:798`, `:803`, `:933`, `:967`, `:996`, `:1026`, `:1047`, `:1090`), so `vote_for_post` is the ONLY site with the two-arm asymmetry and the other TEN — `delete_post:757` and `lock_post:933` among them — are the ABSENCE of it, a different defect. Registered whole as D403; see D403's body and the design spec's CORRECTED Register bullet. The lesson for a plan templated off this one: never write an enumeration into a register instruction without re-deriving it first.**
10. `subscribe_post:116`'s `.one()`, raising `NoResultFound` where the module's convention is a 404.
11. `extra_rate_limit_check` near-duplicated at `app/shared/reply.py:134-139`, the two bodies differing only in the docstring's noun.
12. `app/post/routes.py:280`'s precedence bug: `[post.id] + post.cross_posts if post.cross_posts is not None else []` binds the conditional to the whole expression, so a post with no cross-posts yields `[]` and is never marked read. Outside this module and outside this round.
13. `tests/factories.py`'s two stale line citations, found while writing this plan: `:668` cites `app/models.py:933` for the `read_posts` table, which is at `:942`, and `:682` cites `:942` for `hidden_posts`, which is at `:951`. Both are off by nine. Correct them in `tests/factories.py` as part of this task, since a stale citation in a factory docstring is what sends the next round to the wrong line.
14. Whatever the mutation pass found, including the two recorded survivors.

- [ ] **Step 6: Write the `tests/README.md` facts**

Append starting at **206**, following the existing numbered-heading style. At minimum: the request-context requirement on both source arms; the `community.private`-not-`local_only` federation lever and why `local_only` is a trap; the `mark_post_read` and `hide_post` name collisions between `tests/factories.py` and `app/shared/post.py`; and the `votes_cast_today` redis-key technique for raising the vote quota without database rows.

- [ ] **Step 7: Run the full suite**

**The controller runs this, in the foreground, unpiped, one pytest session at a time.**

```bash
./run_tests.sh
echo "exit=$?"
```

Then, as the separate chained step documented at `tests/README.md:403-405`:

```bash
./run_tests.sh --cov=app --cov-branch --cov-report=json:/tmp/floors34.json && \
  podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py /tmp/floors34.json
```

> **CORRECTED POST-HOC, same defect as Step 2 above** — `./run_tests.sh --exec`
> does not exist and rejects with pytest exit 4. See the note at Step 2 and
> `tests/README.md:403-405`.

The `&&` matters: a failed pytest must not leave a stale report standing for the floor check to pass against.

Record: passed, skipped, duration, exit code, and that all floors were met against a report whose mtime falls at the end of the run.

- [ ] **Step 8: Commit**

```bash
git add coverage_floors.ini docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md tests/factories.py
git commit -F <message-file>
```

Subject: `docs: raise post.py's floor and register sub-project 34's findings`

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the harness section to Task 1's five probes; the coverage-per-function list to Tasks 1-6 and 8; PC1 to Task 9, PC2 to Task 10, PC3 to Task 7; the verification section to Tasks 11 and 12; the register list to Task 12 Step 5, entry for entry. The spec's "every test must be able to fail" requirement appears as a named step in Tasks 2 and 3 and as an assertion requirement in every abort test.

**Placeholder scan.** No step says "add appropriate error handling" or "write tests for the above". Every code step carries the code. Task 10 Step 4 branches on the Step 1 enumeration's result and gives both branches in full rather than deferring one.

**Type consistency.** `_seed()` returns the same `SimpleNamespace` shape in every task that consumes it. `_web_ctx(app, user, query_string='')` keeps its third parameter from Task 1 through Task 8. `_seed_poll(s, mode, choices)` is defined in Task 8 and consumed unchanged in Tasks 9 and 10. `_set_votes_cast_today(app, user_id, count)` is defined in Task 6 and consumed in Task 7. `_bearer(user)` is defined in Task 1 and used throughout.

**Known gap, deliberate.** `:50` and `:1152`'s second disjunct, `user_ip_banned()`, is not pinned by any test — Task 6 Step 4 records the decision and Task 11 sites 12 and 35 expect the survivors. This is the plan's one recorded uncovered conjunct, and it is recorded rather than hidden.

# Sub-project 5e: the Create/Update arm — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover the last uncovered arm of `process_inbox_request` — `Create`/`Update`, 58 statements at 25.9% — plus four statements 5a left in neighbouring arms and one in the preamble, so the function finishes.

**Architecture:** Tests drive `process_inbox_request` through `dispatch()`, seeding rows with `tests/factories.py` and doubling each delegate at its binding site on `app.activitypub.routes`. One new test file for the arm; two existing files gain the neighbouring statements. Defects are pinned first, then fixed test-first in their own commits.

**Tech Stack:** pytest, pytest-timeout, respx, fakeredis, SQLAlchemy, Flask, podman-compose (`./run_tests.sh`).

**Spec:** `docs/superpowers/specs/2026-09-01-coverage-inbox-create-update-5e-design.md`

## Global Constraints

- Defects found **in this arm** are fixed test-first, each in its own commit, separate from every test-only commit, each proved by a mutation that fails a named test. Anything outside the arm is **registered, not fixed**.
- Findings are numbered from **D113** in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`; the index note ("Next free number") is updated in the same change that takes the numbers.
- The coverage floor in `coverage_floors.ini` rises to the measured blended figure **rounded down**, and only ever rises.
- **Locate every code target by content, not by the line numbers in this plan.** They drift.
- No assertion may rest on a column's declared default. Seed an explicit contrary baseline first.
- Every guard is mutation-tested with **each conjunct dropped separately**, each killed by a distinct named test. A kill by `respx.models.AllMockedAssertionError` is an **infrastructure kill, not behavioural**.
- **A docstring claiming a sibling test proves something must be verified true.** 5d lost three fix rounds to false claims of exactly this kind.
- **Never double `find_actor_or_create_cached` unconditionally** — the preamble resolves the activity's own signed actor through it before any arm runs. Scope such doubles to one URL. (`tests/README.md` fact 17.)
- **One pytest session at a time.** The suite assumes exclusive access to the stack. (`tests/README.md` fact 18.)
- Implementers run only the files they touch. **The controller runs the full suite and supplies all coverage figures.**

---

## File structure

| File | Responsibility |
|---|---|
| `tests/factories.py` | gains `make_poll`, `make_poll_choice` |
| `tests/test_inbox_dispatch_create_update.py` | **new** — the whole Create/Update arm |
| `tests/test_inbox_dispatch_votes.py` | gains the `PollVote` and `ChooseAnswer` arm bodies, beside their `Like`/`Dislike` siblings |
| `tests/test_inbox_dispatch_preamble.py` | gains the `'Unexpected activity from Group'` refusal |
| `app/activitypub/routes.py` | defect fixes only, Create/Update arm only |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | findings from D113 |
| `tests/README.md`, `coverage_floors.ini` | harness facts, floor raise |

**Shared helpers — reuse, do not rebuild:**

- `dispatch(activity, store_ap_json=True)` — `tests/test_inbox_dispatch_preamble.py`
- `record_moderation(monkeypatch, *names)` — `tests/test_inbox_dispatch_lock_delete.py`; doubles each name on `app.activitypub.routes` and records `(args, kwargs)`
- `inbox_activity(actor, *, activity_type='Like', object_uri=None, **fields)` — `tests/factories.py`. `**fields` is applied last, so `object={...}` replaces the default string object.
- `seed_community_owner(domain) -> Instance` runs **before** `make_community(host=domain)`; it takes a domain string and calls `make_instance` itself. Moderator status via `make_community_member(mod, community, is_moderator=True)`.
- `make_feed(instance, name='peerfeed', public=False, local=False, with_keys=False)` — instance first.
- `make_post(community, user, ap_id, title='a post', private=False, microblog=False)`

**Delegate signatures**, all patchable on `app.activitypub.routes`:

```python
verify_object_from_source(request_json) -> Tuple[dict|None, str|None]   # util.py:4440
process_chat(user, store_ap_json, core_activity, session)               # DEFINED in routes.py:2505
process_new_content(user, community, store_ap_json, request_json, announced)  # DEFINED in routes.py:2274
find_community(request_json)                                            # util.py:4555
ensure_domains_match(activity: dict) -> bool                            # util.py:4145
update_post_from_activity(post: Post, request_json: dict)               # util.py:3109
refresh_community_profile(community_id, activity_json=None)             # util.py:776
task_selector(task_key, send_async=True, **kwargs)                      # app/shared/tasks/__init__.py:4
process_poll_vote(user, store_ap_json, request_json, announced)         # DEFINED in routes.py:2439
process_question_answer(user, store_ap_json, request_json, announced)   # DEFINED in routes.py:2466
```

**Log constants:** `APLOG_CREATE = (True, 'Create')`, `APLOG_UPDATE = (True, 'Update')`, `APLOG_NOTYPE = (True, 'Unknown')`. Tests assert `log.result` (`'success'`, `'failure'`, `'ignored'`) and `log.exception_message`.

**Poll models** (`app/models.py`): `Poll` is keyed by `post_id` as its **primary key** (:3744-3751), which is why the arm looks it up with `session.query(Poll).get(post.id)`. `PollChoice` (:3783-3790) has its own `id`, a `post_id`, `choice_text` and `num_votes`. `PollChoiceVote` (:3796) is keyed by `(choice_id, user_id)`.

---

### Task 1: Poll factories

**Files:** Modify `tests/factories.py`; create `tests/test_factories_poll.py`

**Interfaces — Produces:**
```python
make_poll(post, *, mode='single', local_only=False, end_poll=None) -> Poll
make_poll_choice(post, choice_text, *, sort_order=0) -> PollChoice
```

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_factories_poll.py"""
from app.models import Poll, PollChoice
from tests.factories import (make_poll, make_poll_choice, make_post, make_user,
                             seed_community_owner, make_community)


def test_make_poll_is_keyed_by_its_post_and_finds_its_choices(app, db_session):
    """`Poll.post_id` is the PRIMARY KEY (app/models.py:3744-3745), not a plain
    FK, which is why the dispatcher looks a poll up with
    `session.query(Poll).get(post.id)` rather than filtering. The factory must
    therefore key on the post it is given, and this test proves that lookup
    shape works rather than merely that a row exists.

    `choice_text` is what the Create/Update arm matches a vote against, so the
    two choices below carry distinct text.
    """
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    post = make_post(community, author, 'https://peer.example/post/1')

    poll = make_poll(post, mode='multiple')
    first = make_poll_choice(post, 'yes', sort_order=0)
    second = make_poll_choice(post, 'no', sort_order=1)

    assert db_session.query(Poll).get(post.id) is poll
    assert poll.mode == 'multiple'          # explicitly passed, not a default
    assert poll.local_only is False

    texts = {c.choice_text for c in db_session.query(PollChoice).filter_by(post_id=post.id)}
    assert texts == {'yes', 'no'}
    assert first.num_votes == 0 and second.num_votes == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./run_tests.sh tests/test_factories_poll.py -q`
Expected: FAIL, `ImportError: cannot import name 'make_poll'`

- [ ] **Step 3: Add the factories**

Add `Poll`, `PollChoice` to the `app.models` import block in `tests/factories.py` (alphabetical position), then:

```python
def make_poll(post: Post, *, mode: str = 'single', local_only: bool = False,
              end_poll: datetime = None) -> Poll:
    """The Poll row the Create/Update arm resolves with
    `session.query(Poll).get(post.id)`.

    `post_id` is Poll's PRIMARY KEY (app/models.py:3745), so a post has at most
    one poll and the identity map returns the same object for the same post --
    which is what makes the `.get()` lookup in the arm work at all.

    `end_poll` defaults to None rather than a future date: nothing on the
    dispatcher's vote path consults it, and inventing a deadline here would put
    a value in the fixture that no test asserts on.
    """
    poll = Poll(post_id=post.id, mode=mode, local_only=local_only, end_poll=end_poll)
    db.session.add(poll)
    db.session.commit()
    return poll


def make_poll_choice(post: Post, choice_text: str, *, sort_order: int = 0) -> PollChoice:
    """One option on `post`'s poll.

    The Create/Update arm matches an incoming vote by `choice_text` against the
    `name` field of the inbound Note, so callers should pass the exact text the
    activity will carry.
    """
    choice = PollChoice(post_id=post.id, choice_text=choice_text, sort_order=sort_order)
    db.session.add(choice)
    db.session.commit()
    return choice
```

Add `from datetime import datetime` to the file's imports if it is not already there.

- [ ] **Step 4: Run it and watch it pass**

Run: `./run_tests.sh tests/test_factories_poll.py -q` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/factories.py tests/test_factories_poll.py
git commit -m "test: add Poll and PollChoice factories for the Create/Update arm"
```

---

### Task 2: the string-object and ChatMessage branches

**Files:** Create `tests/test_inbox_dispatch_create_update.py`

**Interfaces — Produces:** `create_activity(actor, obj, *, activity_type='Create', **outer)` used by every later task in this file.

The arm's first two branches. `verify_object_from_source` returns `(dict|None, str|None)`; on `None` the arm logs FAILURE naming the reason and returns. `ChatMessage` delegates to `process_chat` and returns.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_inbox_dispatch_create_update.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog
from tests.factories import (inbox_activity, make_community, make_instance, make_post,
                             make_user, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def create_activity(actor, obj, *, activity_type='Create', **outer):
    """A Create (or Update) whose `object` is `obj`.

    `inbox_activity` applies **fields last, so passing `object=` replaces its
    default string object. `obj` may be a dict OR a bare string -- the arm's
    first branch exists precisely to handle the string form.
    """
    return inbox_activity(actor, activity_type=activity_type, object=obj, **outer)


def test_an_unverifiable_string_object_logs_the_refusal_reason(app, db_session, monkeypatch):
    """`isinstance(core_activity['object'], str)` sends the activity to
    `verify_object_from_source`, which returns `(None, reason)` on refusal. The
    arm puts that reason into the log rather than a generic sentence, which is
    the whole point of the delegate returning it -- so the assertion pins the
    reason, not merely the failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'verify_object_from_source',
                        lambda activity: (None, 'host mismatch'))

    dispatch(create_activity(author, 'https://peer.example/objects/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Could not verify unsigned request from source: host mismatch'


def test_a_verified_string_object_continues_into_the_normal_path(app, db_session, monkeypatch):
    """On success `verify_object_from_source` returns the activity with its
    `object` replaced by the fetched document, and processing continues. The
    double returns a ChatMessage object so the continuation is observable via
    `process_chat` without also exercising the content path.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')

    def fake_verify(activity):
        activity['object'] = {'type': 'ChatMessage', 'id': 'https://peer.example/pm/1'}
        return activity, None

    monkeypatch.setattr(activitypub_routes, 'verify_object_from_source', fake_verify)
    calls = record_moderation(monkeypatch, 'process_chat')

    dispatch(create_activity(author, 'https://peer.example/objects/1'))

    assert len(calls['process_chat']) == 1


def test_a_chat_message_object_delegates_to_process_chat_and_returns(app, db_session, monkeypatch):
    """The `ChatMessage` branch delegates and returns immediately. `user` is the
    signed outer actor, and `session` is the dispatcher's own task session --
    both asserted positionally here, because the delegate's signature is
    `process_chat(user, store_ap_json, core_activity, session)` and a later
    reader cannot otherwise tell which argument is which.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    calls = record_moderation(monkeypatch, 'process_chat', 'process_new_content')

    activity = create_activity(author, {'type': 'ChatMessage', 'id': 'https://peer.example/pm/1'})
    dispatch(activity)

    assert len(calls['process_chat']) == 1
    args, kwargs = calls['process_chat'][0]
    from sqlalchemy import inspect as sa_inspect
    assert sa_inspect(args[0]).identity[0] == author.id
    assert args[2] is activity
    assert calls['process_new_content'] == []
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_create_update.py -q`
Expected: PASS. A failure here is a finding — report it and assert what the code actually does rather than adjusting the code.

- [ ] **Step 3: Mutation-test the string check**

Replace `isinstance(core_activity['object'], str)` with `False` and confirm `test_an_unverifiable_string_object_logs_the_refusal_reason` fails. Restore; verify `git diff app/activitypub/routes.py` is empty. Record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_create_update.py
git commit -m "test: cover Create/Update's string-object and ChatMessage branches"
```

---

### Task 3: the poll-vote guard's five conjuncts

**Files:** Modify `tests/test_inbox_dispatch_create_update.py`

The guard is `object['type'] == 'Note'` **and** `'name' in object` **and** `'inReplyTo' in object` **and** `'attributedTo' in object` **and** `'published' not in object`. Five conjuncts, five distinct kills. This task proves the *selection*; Task 4 covers what happens once selected.

**Interfaces — Produces:** `poll_note(post_ap_id, choice_text, **extra)` and `seed_poll_post()`, reused by Task 4.

- [ ] **Step 1: Write the failing tests**

```python
def seed_poll_post(host='peer.example', choice_text='yes', local_author=False):
    """A post carrying a poll with one choice, plus the voter.

    Returns (instance, voter, post, poll, choice). `local_author` controls
    whether the POST's author is local, which is what the arm's
    `post_being_replied_to.author.is_local()` branch keys off -- not the voter.
    """
    instance = seed_community_owner(host)
    community = make_community(host=host)
    if local_author:
        author = make_user(None, 'localauthor', local=True)
    else:
        author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    post = make_post(community, author, f'https://{host}/post/1')
    poll = make_poll(post)
    choice = make_poll_choice(post, choice_text)
    db.session.commit()
    return instance, voter, post, poll, choice


def poll_note(post_ap_id, choice_text, **extra):
    """The exact object shape the poll-vote guard selects: a Note carrying a
    `name`, an `inReplyTo` and an `attributedTo`, and NO `published`.

    `**extra` lets a test add or override one field to break exactly one
    conjunct, which is how the five mutation kills below stay independent.
    """
    obj = {'type': 'Note', 'name': choice_text, 'inReplyTo': post_ap_id,
           'attributedTo': 'https://peer.example/u/voter'}
    obj.update(extra)
    return obj


def test_a_poll_shaped_note_is_selected_and_records_the_vote(app, db_session, monkeypatch):
    """All five conjuncts true. The vote is asserted through PollChoiceVote
    rather than through the delegate, because the arm calls
    `poll_data.vote_for_choice(...)` directly rather than a doubled function.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post()
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    from app.models import PollChoiceVote
    db.session.expire_all()
    vote = db_session.query(PollChoiceVote).filter_by(user_id=voter.id).one()
    assert vote.choice_id == choice.id
    assert calls['process_new_content'] == []   # selected the poll path, not content

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


@pytest.mark.parametrize('breaker,description', [
    ({'type': 'Article'}, 'type is not Note'),
    ({'name': None}, 'name absent'),
    ({'inReplyTo': None}, 'inReplyTo absent'),
    ({'attributedTo': None}, 'attributedTo absent'),
    ({'published': '2026-01-01T00:00:00Z'}, 'published present'),
])
def test_breaking_any_one_conjunct_leaves_the_poll_path(app, db_session, monkeypatch, breaker, description):
    """Each parametrisation breaks exactly ONE of the guard's five conjuncts and
    asserts the activity no longer takes the poll path -- it reaches
    `process_new_content` instead (a Note is in new_content_types).

    A `None` value in `breaker` means "delete this key", since four of the five
    conjuncts are membership tests rather than value tests.

    This is what makes each conjunct independently load-bearing: five separate
    parametrisations, five separate failures if any conjunct is dropped.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post()
    obj = poll_note(post.ap_id, 'yes')
    for key, value in breaker.items():
        if value is None:
            obj.pop(key)
        else:
            obj[key] = value

    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *a, **k: False)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(voter, obj))

    from app.models import PollChoiceVote
    assert db_session.query(PollChoiceVote).count() == 0, description
    assert len(calls['process_new_content']) == 1, description
```

Add `import pytest` and `from tests.factories import make_poll, make_poll_choice` to the file's imports.

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_create_update.py -q` — Expected: PASS

- [ ] **Step 3: Mutation-test all five conjuncts separately**

Drop each conjunct from the guard in turn (five separate edits), confirming each time that the matching parametrisation of `test_breaking_any_one_conjunct_leaves_the_poll_path` fails. Restore after each; verify `git diff app/activitypub/routes.py` is empty before committing. **Record all five kills separately in your report.** If any conjunct cannot be killed, say so — that is a finding, not something to work around.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_create_update.py
git commit -m "test: pin all five conjuncts of the poll-vote guard"
```

---

### Task 4: the poll-vote path's four outcomes, and its silence

**Files:** Modify `tests/test_inbox_dispatch_create_update.py`

Four outcomes: post not found; poll not found; choice not found; full success (and, for a local post author, an `edited_at` stamp plus `task_selector('edit_post', ...)`). **Three of the four log nothing at all** — pin that, do not fix it here.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_poll_vote_for_an_unknown_post_is_dropped_silently(app, db_session, monkeypatch):
    """`post_being_replied_to` is None, so the block falls to its unconditional
    `return` having logged NOTHING -- asserted with LOG_ACTIVITYPUB_TO_DB
    explicitly True so the zero is real silence, not logging switched off.

    It also does NOT fall through to content handling: `process_new_content` is
    doubled and must not be called. That combination -- consumed, unlogged,
    unprocessed -- is the finding this test exists to pin.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post()
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(voter, poll_note('https://peer.example/post/404', 'yes')))

    assert ActivityPubLog.query.count() == 0
    assert calls['process_new_content'] == []


def test_a_poll_vote_on_a_post_with_no_poll_is_dropped_silently(app, db_session, monkeypatch):
    """`poll_data` is None: the post exists but carries no Poll row. Same
    silence as above, reached by a different conjunct of `if poll_data and
    choice:` -- which is why this test and the next are separate.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    assert ActivityPubLog.query.count() == 0


def test_a_poll_vote_for_an_unknown_choice_is_dropped_silently(app, db_session, monkeypatch):
    """`choice` is None: the poll exists but has no option with this `name`.
    The other conjunct of `if poll_data and choice:`.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post(choice_text='yes')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'maybe')))

    from app.models import PollChoiceVote
    assert db_session.query(PollChoiceVote).count() == 0
    assert ActivityPubLog.query.count() == 0


def test_a_poll_vote_on_a_local_authors_post_stamps_it_and_schedules_an_edit(app, db_session, monkeypatch):
    """`post_being_replied_to.author.is_local()` -- the LOCAL branch. `edited_at`
    is seeded to a stale value first, so the stamp is evidence of the write
    rather than a default sitting there. `task_selector` is doubled and its
    kwargs asserted, because the task key and post id are a contract with the
    background worker.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post(local_author=True)
    stale = utcnow() - timedelta(days=3)
    post.edited_at = stale
    db.session.commit()
    post_id = post.id

    calls = record_moderation(monkeypatch, 'task_selector')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).edited_at > stale
    assert len(calls['task_selector']) == 1
    args, kwargs = calls['task_selector'][0]
    assert args[0] == 'edit_post'
    assert kwargs['post_id'] == post_id


def test_a_poll_vote_on_a_remote_authors_post_neither_stamps_nor_schedules(app, db_session, monkeypatch):
    """The other side of `is_local()`. Paired with the test above so the guard
    cannot be dropped in either direction. `edited_at` is seeded stale and must
    stay exactly stale.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, voter, post, poll, choice = seed_poll_post(local_author=False)
    stale = utcnow() - timedelta(days=3)
    post.edited_at = stale
    db.session.commit()
    post_id = post.id

    calls = record_moderation(monkeypatch, 'task_selector')

    dispatch(create_activity(voter, poll_note(post.ap_id, 'yes')))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).edited_at == stale
    assert calls['task_selector'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
```

Add `from datetime import timedelta` and `from app.models import utcnow` to the imports.

- [ ] **Step 2: Run the file** — `./run_tests.sh tests/test_inbox_dispatch_create_update.py -q`, Expected: PASS

- [ ] **Step 3: Mutation-test the `is_local()` guard**

Remove `if post_being_replied_to.author.is_local():` (de-indent its body) and confirm `test_a_poll_vote_on_a_remote_authors_post_neither_stamps_nor_schedules` fails. Restore; record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_create_update.py
git commit -m "test: cover the poll-vote path's four outcomes and pin its silence"
```

---

### Task 5: the community-resolution chain

**Files:** Modify `tests/test_inbox_dispatch_create_update.py`

Guarded by `if not announced and not community:`. Inside: `find_community`; if none, `process_chat` (return if truthy); `ensure_domains_match` else FAILURE `'Domains do not match'`; `community.local_only` else FAILURE `'Remote Create in local_only community'`.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_unresolvable_community_falls_back_to_process_chat_and_returns(app, db_session, monkeypatch):
    """`find_community` returns None, so `process_chat` is tried; a truthy
    return means it handled the activity and the arm returns immediately --
    proved here by `ensure_domains_match` never being reached.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *a, **k: True)
    calls = record_moderation(monkeypatch, 'ensure_domains_match')

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert calls['ensure_domains_match'] == []


def test_a_falsy_process_chat_continues_into_the_domain_check(app, db_session, monkeypatch):
    """The other side: `process_chat` returns falsy, so the arm does NOT return
    and reaches `ensure_domains_match`. Paired with the test above so the
    `if process_chat(...)` guard cannot be dropped in either direction.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *a, **k: False)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: False)

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Domains do not match'


def test_a_mismatched_domain_is_refused(app, db_session, monkeypatch):
    """`ensure_domains_match` False -> FAILURE, and `process_new_content` is
    never reached.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: False)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert calls['process_new_content'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Domains do not match'


def test_a_remote_create_into_a_local_only_community_is_refused(app, db_session, monkeypatch):
    """`community.local_only` -- seeded explicitly True, since the column's
    default is False and an assertion resting on that would prove nothing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    community.local_only = True
    db.session.commit()
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert calls['process_new_content'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Remote Create in local_only community'
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test `local_only`'s two conjuncts**

`if community and community.local_only:` has two conjuncts. Drop `community and` and confirm a named test still passes or fails — if nothing changes, that is a finding (the guard's first half may be unreachable here), and you must report it rather than inventing a test to justify it. Then drop `community.local_only` and confirm `test_a_remote_create_into_a_local_only_community_is_refused` fails. Restore; record both results.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_create_update.py
git commit -m "test: cover Create/Update's community-resolution chain"
```

---

### Task 6: the object-type dispatch — new content, and the unacceptable-type fallthrough

**Files:** Modify `tests/test_inbox_dispatch_create_update.py`

`new_content_types = ['Page', 'Article', 'Link', 'Note', 'Question', 'Event']`. Anything not matched by any branch logs FAILURE `'Unacceptable type (create): ' + object_type`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize('object_type', ['Page', 'Article', 'Link', 'Question', 'Event'])
def test_each_new_content_type_reaches_process_new_content(app, db_session, monkeypatch, object_type):
    """Every member of `new_content_types` except 'Note', which is covered
    separately because a bare Note without the poll fields also reaches here
    (see the poll-guard tests) and parametrising it twice would obscure that.

    `announced` is passed through to the delegate, so it is asserted: this
    activity is not announced, so the fifth positional argument must be False.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': object_type, 'id': 'https://peer.example/post/1'}))

    assert len(calls['process_new_content']) == 1
    args, kwargs = calls['process_new_content'][0]
    assert args[4] is False        # announced


def test_an_unacceptable_object_type_names_itself_in_the_failure(app, db_session, monkeypatch):
    """The fallthrough. The type is concatenated into the message, so the
    assertion pins the whole string rather than just the failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)

    dispatch(create_activity(author, {'type': 'Tombstone', 'id': 'https://peer.example/x/1'}))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unacceptable type (create): Tombstone'


def test_a_create_of_a_group_is_unacceptable_because_the_group_branch_requires_update(app, db_session, monkeypatch):
    """`elif object_type == 'Group' and core_activity['type'] == 'Update'` --
    the second conjunct. A CREATE of a Group therefore falls through to the
    unacceptable-type log rather than refreshing a community profile. This is
    the test that kills that conjunct.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile')

    dispatch(create_activity(author, {'type': 'Group', 'id': community.ap_profile_id}))

    assert calls['refresh_community_profile'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Unacceptable type (create): Group'
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_inbox_dispatch_create_update.py
git commit -m "test: cover Create/Update's new-content and unacceptable-type paths"
```

---

### Task 7: the PeerTube Video branch and the Group/Update branch

**Files:** Modify `tests/test_inbox_dispatch_create_update.py`

Video: post found and owned → `update_post_from_activity` + SUCCESS; found, not owned → FAILURE `'Edit attempt denied'`; not found → FAILURE `'PeerTube post not found'`. Group+Update: local community edited by a non-moderator → FAILURE `'Comm edit by non-moderator'`; otherwise `refresh_community_profile` + SUCCESS, and for a **local** community also `announce_activity_to_followers`.

- [ ] **Step 1: Write the failing tests**

```python
def _seed_video_post(host='peer.example'):
    instance = seed_community_owner(host)
    community = make_community(host=host)
    owner = make_user(instance, 'owner')
    post = make_post(community, owner, f'https://{host}/videos/watch/1')
    db.session.commit()
    return instance, community, owner, post


def test_a_peertube_video_edit_by_its_owner_updates_the_post(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, owner, post = _seed_video_post()
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(create_activity(owner, {'type': 'Video', 'id': post.ap_id}, activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_peertube_video_edit_by_another_user_is_denied(app, db_session, monkeypatch):
    """`user.id == post.user_id` is the ownership test; a different actor is
    refused. Paired with the test above so the guard dies in both directions.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, owner, post = _seed_video_post()
    interloper = make_user(instance, 'interloper')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(create_activity(interloper, {'type': 'Video', 'id': post.ap_id}, activity_type='Update'))

    assert calls['update_post_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Edit attempt denied'


def test_a_peertube_video_edit_for_an_unknown_post_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, owner, post = _seed_video_post()
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)

    dispatch(create_activity(owner, {'type': 'Video', 'id': 'https://peer.example/videos/watch/404'},
                             activity_type='Update'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'PeerTube post not found'


def test_a_group_update_from_a_non_moderator_of_a_LOCAL_community_is_refused(app, db_session, monkeypatch):
    """`community.is_local() and not community.is_moderator(user)`. Both
    conjuncts matter: this test supplies the local half, and the remote-community
    test below supplies the other.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    local_community = make_community(host=app.config['SERVER_NAME'])
    outsider = make_user(instance, 'outsider')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: local_community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile')

    dispatch(create_activity(outsider, {'type': 'Group', 'id': local_community.ap_profile_id},
                             activity_type='Update'))

    assert calls['refresh_community_profile'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Comm edit by non-moderator'


def test_a_group_update_of_a_REMOTE_community_refreshes_without_announcing(app, db_session, monkeypatch):
    """A remote community: `is_local()` is False, so the permission guard's
    first conjunct short-circuits and the refresh runs. `announce_activity_to_followers`
    is gated on `community.is_local()` a second time, so it must NOT fire here --
    which is what distinguishes this test from a local-community success.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    remote_community = make_community(host='peer.example')
    editor = make_user(instance, 'editor')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: remote_community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    calls = record_moderation(monkeypatch, 'refresh_community_profile',
                              'announce_activity_to_followers')

    dispatch(create_activity(editor, {'type': 'Group', 'id': remote_community.ap_profile_id},
                             activity_type='Update'))

    assert len(calls['refresh_community_profile']) == 1
    assert calls['announce_activity_to_followers'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
```

- [ ] **Step 2: Run the file** — Expected: PASS. If `make_community(host=app.config['SERVER_NAME'])` does not produce a community `is_local()` accepts, read `Community.is_local()` and adjust the seeding — report what you found.

- [ ] **Step 3: Mutation-test the ownership and Group guards**

Drop `user.id == post.user_id` and confirm the denial test fails. Restore. Drop `community.is_local()` from the permission guard and confirm a named test fails. Restore. Record both kills.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_create_update.py
git commit -m "test: cover Create/Update's PeerTube Video and Group/Update branches"
```

---

### Task 8: pin the feed-Announce crash surface

**Files:** Modify `tests/test_inbox_dispatch_create_update.py`

**This task asserts BROKEN behaviour. Do not fix anything here** — Task 9 inverts these tests and lands the fixes, which is how each fix earns its proof.

When an Announce's outer actor resolves to a **feed**, the preamble's `if not feed:` takes its `else`, leaving `user = None`, and `community` was never set — while `announced` becomes `True`. Create/Update then skips its whole resolution chain, which is guarded by `if not announced and not community:`.

**Verify this reachability before writing the tests.** Read the preamble's Announce handling and confirm a feed-actor Announce genuinely arrives at the Create/Update arm with both `user` and `community` as `None`. If it does not — if some earlier check rejects it — then this spec's central claim is wrong, and you must **report that rather than contriving a test to make it true**.

- [ ] **Step 1: Write tests pinning current behaviour**

5a already built this seeding shape as `_seed_announcing_feed` in
`tests/test_inbox_dispatch_announce.py` — read it, then add the equivalent here:

```python
def _seed_feed_announcer(host='peer.example'):
    """A Feed resolvable as the OUTER Announce actor.

    This is the whole crash surface in one fixture. When the preamble resolves
    an Announce's actor to a FEED, its `if not feed:` takes the else, so `user`
    is left None -- and `community` was never set either -- while `announced`
    becomes True. 5a proved this shape reachable in
    tests/test_inbox_dispatch_announce.py::_seed_announcing_feed.
    """
    make_site()
    instance = make_instance(host)
    feed = make_feed(instance)
    return instance, feed


def announced_create(feed, inner_object, *, inner_type='Create'):
    """An Announce sent BY a feed, wrapping a Create/Update.

    The preamble sets `core_activity = request_json['object']`, so the inner
    dict IS the Create activity the arm then dispatches on. The inner `actor`
    is deliberately absent: it is read only under `if not feed:`, which a feed
    Announce skips, and including one would imply it mattered.
    """
    return inbox_activity(feed, activity_type='Announce',
                          object={'id': f'{feed.ap_profile_id}/activities/inner',
                                  'type': inner_type,
                                  'object': inner_object})


def test_a_feed_announced_group_update_crashes_on_a_none_community(app, db_session, monkeypatch):
    """PINS the Group half of the crash. `announced` is True, so the arm skips
    its `if not announced and not community:` resolution chain entirely and
    `community` is still None at `community.is_local()`.

    `refresh_community_profile` is NOT doubled: the AttributeError fires while
    Python evaluates the guard, before any delegate is reached, so doubling
    would change nothing and would only obscure what is being observed.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed = _seed_feed_announcer()

    activity = announced_create(feed,
                                {'type': 'Group', 'id': 'https://peer.example/c/books'},
                                inner_type='Update')

    with pytest.raises(AttributeError):
        dispatch(activity)


def test_a_feed_announced_poll_vote_crashes_on_a_none_user(app, db_session, monkeypatch):
    """PINS the poll half. The crash is at `vote_for_choice(choice.id, user.id)`,
    which is reached only once a post, its poll and a matching choice all
    resolve -- so all three are seeded here. A test that seeded less would pass
    for the wrong reason, by returning early before ever touching `user`.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed = _seed_feed_announcer()
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    post = make_post(community, author, 'https://peer.example/post/1')
    make_poll(post)
    make_poll_choice(post, 'yes')
    db.session.commit()

    activity = announced_create(feed, poll_note(post.ap_id, 'yes'))

    with pytest.raises(AttributeError):
        dispatch(activity)


def test_a_feed_announced_page_hands_process_new_content_two_nones(app, db_session, monkeypatch):
    """PINS the third consequence, which does NOT crash here: the content path
    is simply handed `user=None` and `community=None` and left to cope. The
    delegate is doubled and its first two positional arguments asserted, so the
    finding is recorded as what is actually passed rather than as speculation
    about what the delegate does with it.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, feed = _seed_feed_announcer()
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(announced_create(feed, {'type': 'Page', 'id': 'https://peer.example/post/1'}))

    assert len(calls['process_new_content']) == 1
    args, kwargs = calls['process_new_content'][0]
    assert args[0] is None        # user
    assert args[1] is None        # community
    assert args[4] is True        # announced
```

Add `make_feed`, `make_site` to the file's factory imports.

**Before writing these, verify the reachability claim.** Read the preamble's
Announce handling and confirm a feed-actor Announce genuinely arrives at the
Create/Update arm with both `user` and `community` as `None`. If some earlier
check rejects it first, this plan's central claim is wrong: **report that
rather than contriving a test to make it true.**

- [ ] **Step 2: Run the file** — Expected: PASS, since every assertion describes current behaviour. If one fails, the defect is not what the spec says; report that.

- [ ] **Step 3: Commit**

```bash
git add tests/test_inbox_dispatch_create_update.py
git commit -m "test: pin the feed-Announce crash surface in Create/Update"
```

---

### Task 9: fix the feed-Announce crashes and the poll-vote silence

**Files:** Modify `app/activitypub/routes.py` (Create/Update arm only); modify `tests/test_inbox_dispatch_create_update.py`

Two fixes, **two separate commits**, each preceded by inverting its tests and watching them fail.

**Fix A — the feed-Announce crash.** The arm must not dereference a `None` `community` or `None` `user`. The minimal shape consistent with the arm's own intent is to refuse the activity rather than crash: when the arm reaches the `object_type` dispatch without a `community`, log a FAILURE naming the situation and return, in the same style as the arm's other refusals. **Decide the exact condition and message yourself from the code**, and state your reasoning in the report — the plan deliberately does not dictate it, because the right guard depends on what Task 8 observed. Do not change the preamble; that is a different arm and out of scope. If the right fix looks like it belongs in the preamble, register that as a finding instead.

**Fix B — the poll-vote silence.** Give each of the three silent outcomes a log, in the arm's established idiom (`log_incoming_ap(id, APLOG_CREATE, APLOG_IGNORED, saved_json, '<reason>')`). Distinct messages, so an operator can tell the three apart. Invert Task 4's three silence tests to assert the new logs.

- [ ] **Step 1: Invert the Fix A tests, run, and watch them fail**
- [ ] **Step 2: Apply Fix A**
- [ ] **Step 3: Run and watch them pass**
- [ ] **Step 4: Mutation-test Fix A** — revert it, confirm a named test fails, restore. Record the kill.
- [ ] **Step 5: Commit Fix A** with a message explaining what crashed and why the guard is where it is.
- [ ] **Step 6: Invert the Fix B tests, run, and watch them fail**
- [ ] **Step 7: Apply Fix B**
- [ ] **Step 8: Run and watch them pass**
- [ ] **Step 9: Mutation-test Fix B** — remove one of the three logs, confirm the matching test fails, restore. Record the kill.
- [ ] **Step 10: Commit Fix B**

**Do not fix the unconditional `return` at the end of the poll block.** Whether a poll-shaped activity that resolves nothing should fall through to content handling is a product decision, not a defect with an obvious answer. Register it.

---

### Task 10: the neighbouring statements that finish the function

**Files:** Modify `tests/test_inbox_dispatch_votes.py`, `tests/test_inbox_dispatch_preamble.py`

Four statements 5a left in the vote arms, and two in the preamble.

- [ ] **Step 1: Cover the `PollVote` and `ChooseAnswer` arm bodies**

`tests/test_inbox_dispatch_votes.py` already imports `process_poll_vote` and
`process_question_answer` but never dispatches to them, which is exactly why
these four statements are uncovered. Add, in that file, beside its
`Like`/`Dislike` siblings:

```python
@pytest.mark.parametrize('activity_type,delegate', [
    ('PollVote', 'process_poll_vote'),
    ('ChooseAnswer', 'process_question_answer'),
])
def test_poll_vote_and_choose_answer_dispatch_to_their_own_delegates(
        app, db_session, monkeypatch, activity_type, delegate):
    """The two arms 5a imported delegates for but never exercised. Each passes
    the same four arguments as the Like/Dislike arms above --
    (user, store_ap_json, request_json, announced) -- so this asserts all four
    rather than only that the delegate ran.

    Parametrised across both because the two arms are structurally identical;
    the `delegate` parameter is what keeps each one's own binding site under
    test rather than sharing a double.
    """
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, delegate,
                        lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(actor, activity_type=activity_type)

    dispatch(activity)

    assert len(calls) == 1
    user_arg, store_ap_json_arg, request_json_arg, announced_arg = calls[0]
    assert user_arg is not None and user_arg.id == actor.id
    assert store_ap_json_arg is True
    assert request_json_arg == activity
    assert announced_arg is False
```

Add `import pytest` to that file if it is not already imported.

- [ ] **Step 2: Cover the preamble's `'Unexpected activity from Group'` refusal**

In `tests/test_inbox_dispatch_preamble.py`:

```python
def test_an_unexpected_activity_type_from_a_group_actor_is_refused(app, db_session, monkeypatch):
    """The `else` of the Community-actor chain in the preamble: an actor that
    resolves to a Community sending something that is neither Add, Remove nor
    Update.

    `Like` is chosen because it is a type this dispatcher handles perfectly well
    from a USER actor, so the refusal is demonstrably about the actor being a
    Community rather than about the type being unknown.

    This is NOT the sibling `'Unexpected Update activity from Group'` path,
    which is an Update whose object type is neither Group nor OrderedCollection.
    The two messages differ by one word; only this one is covered here.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    community = make_community(host='peer.example')
    db.session.commit()

    activity = inbox_activity(community, activity_type='Like',
                              object_uri='https://peer.example/post/1')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unexpected activity from Group'
```

Check that file's existing imports and add only what is missing. If the
activity is instead resolved as an Announce or rejected earlier, read the
preamble and adjust the activity type — then say in the docstring which types
do and do not reach this `else`.

- [ ] **Step 3: Run both files**

Run: `./run_tests.sh tests/test_inbox_dispatch_votes.py tests/test_inbox_dispatch_preamble.py -q`
Expected: PASS, with every pre-existing test in both files still passing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_votes.py tests/test_inbox_dispatch_preamble.py
git commit -m "test: cover the PollVote, ChooseAnswer and Group-fallthrough statements"
```

---

### Task 11: the register, the README, and the floor

**Files:** Modify the findings register, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Register the findings**

Add a `## Sub-project 5e` section to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, in the shape sub-project 5d's section uses (**read it first**). Number from **D113** and update the index note's "Next free number" in the same change.

Source the findings from the task reports. Every row needs location, defect, impact, and how it was established — `measured` where a test proves it, `reading-level` where derived by reading. **Do not overstate scope**; the register already carries a public correction of D91 for exactly that, and 5d needed three citation fixes.

Register at minimum, beyond whatever the tasks found:

- The two fixes from Task 9, with their commits.
- **The unconditional `return` at the end of the poll-vote block** — a poll-shaped activity that resolves nothing is consumed rather than falling through to content handling. Deliberately not fixed; say why.
- **`routes.py:860`** — `pass` inside `if actor_id and actor_id.startswith('https://s.rimu.geek.nz'):`, with the comment *"just here to set breakpoints on, during testing. remove before commit"*. Debug scaffolding reachable only from one named personal domain. **Not covered and not removed**, on the reasoning that covering it would entrench code its author meant to delete. Recommend deletion.
- **`process_new_content`'s `if user.user_name == 'rimu': pass`** — the same class of debug artifact, in the delegate this arm calls. Reading-level; recommend deletion.

- [ ] **Step 2: Record the harness facts**

Append to `tests/README.md`'s numbered list, continuing its numbering, whatever this sub-project established that a later one would otherwise rediscover — at minimum that `Poll` is keyed by `post_id` as its primary key, which is why the dispatcher resolves it with `.get(post.id)`.

- [ ] **Step 3: Raise the floor**

The controller supplies the measured blended figure. Set `app/activitypub/routes.py` in `coverage_floors.ini` to that figure **rounded down**. **Do not run the full suite.**

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register 5e's findings and raise the routes.py floor"
```

# Inbox Dispatcher 5a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `process_inbox_request`'s preamble, its dispatch arms and the four vote delegates — 197 statements, 5 currently executed — to full statement and branch coverage with mutation evidence per guard, and register what the tests find.

**Architecture:** Tests call `process_inbox_request(activity, store_ap_json)` directly, which is the production DEBUG branch at `routes.py:758-759`. Actors are seeded as committed rows so the preamble's `find_actor_or_create_cached` resolves off the database. A three-test seam suite drives the same arms through a real signed POST so the register's reachability claims are measured rather than read.

**Tech Stack:** pytest, `podman-compose` via `./run_tests.sh`, `coverage.py` (branch mode), fakeredis, respx.

**Spec:** `docs/superpowers/specs/2026-08-30-coverage-inbox-dispatch-5a-design.md`

## Global Constraints

- **`if TYPE_CHECKING` is always a bug.** Never introduce it; never add it to `exclude_lines`.
- **Imports go at the top of the file. No inline imports.** The campaign adds no new violations.
- **Floors only ever rise.** Lowering a floor to make a run pass is rejected. `coverage_floors.ini` is the ratchet.
- **One suite run at a time per worktree.** Two runs against the same tmpfs database corrupt each other's results.
- **Never run `./run_tests.sh --down` casually.** It destroys the tmpfs volume and replays ~269 migrations.
- **`--cov=app.module` (dotted) works; `--cov=app/module.py` (path) silently measures nothing.**
- **Findings are registered, not fixed.** Fixes are their own change, after the sub-project.
- **A guard is tested on the whole domain it claims to reject**, not on the one example that motivated it.
- **Each half of a compound guard is dropped separately and must die distinctly.**
- `tests/test_activitypub_util.py` (3 tests) needs live network and is excluded from every documented command; suite totals that look 3 short are this.
- Baseline at `4c470978`: **2757 passed, 3 skipped**. `app/activitypub/routes.py` at **310/1813 statements, 89/890 branches, 14.76 % blended, floor 14**.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/test_inbox_dispatch_preamble.py` (create) | Tasks 1–3, 8: entry, actor resolution, the Group branch, the refusals, the seam suite |
| `tests/test_inbox_dispatch_announce.py` (create) | Task 4: the unwrap — string, list, `OrderedCollection`, the inner-actor walk, the banned check |
| `tests/test_inbox_dispatch_votes.py` (create) | Tasks 5–6: the four arms and the four delegates |
| `tests/test_inbox_dispatch_misc.py` (create) | Task 7: Flag, Move, QuoteRequest, the `except`/`finally` |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | Task 9: the register |
| `coverage_floors.ini`, `tests/README.md` (modify) | Task 10: the raised floor, the entry lever documented |

Four test files split by what a test must set up, the same reasoning that gave sub-project 4 three files.

### The shared entry helper

Defined once, in Task 1, in `tests/test_inbox_dispatch_preamble.py`, and imported by the other three files:

```python
def dispatch(activity, store_ap_json=True):
    """Call the dispatcher the way production's DEBUG branch does."""
    from app.activitypub.routes import process_inbox_request
    process_inbox_request(activity, store_ap_json)
```

Task 4 needs it for the recursion tests, Tasks 5–7 for every arm. Import it as
`from tests.test_inbox_dispatch_preamble import dispatch`.

---

### Task 1: The spike that decides whether this sub-project is possible

**Files:**
- Create: `tests/test_inbox_dispatch_preamble.py`

**Interfaces:**
- Produces: `dispatch(activity, store_ap_json=True)` — the entry helper every later task imports.

`process_inbox_request` opens its own app context, takes an independent
`Session(bind=db.engine)` from `get_task_session()`, runs under
`patch_db_session`, and closes that session in `finally`. Three things must hold
before a single arm test is worth writing, and none is safe to assume.

- [ ] **Step 1: Confirm the three preconditions by reading, and record what you read**

Read and write down the answer to each, with the file and line:

1. **Do seeded rows reach the task session?** `tests/conftest.py:117`'s `db_session`
   fixture truncates rather than rolling back — "the code under test calls
   `db.session.commit()` in several places, which a rollback-based fixture would
   have to fight" — and `tests/factories.py` commits. Committed rows are visible
   to an independent session on another connection. **Expected: yes.**
2. **Does `patch_db_session` actually patch here?** `app/utils.py:3664` yields
   *without patching* when `has_request_context()`. A direct call from a test has
   an app context but no request context, so it patches. **Expected: yes for the
   direct call, NO for the Task 8 seam tests**, which run inside a request.
3. **Do `ActivityPubLog` rows written inside the dispatcher become visible to the
   test afterwards?** `log_incoming_ap` (`app/activitypub/util.py:4523`) writes
   through `db.session` — which, under the patch, is the task session — and
   commits. Every later assertion in this sub-project depends on this.

- [ ] **Step 2: Write the spike test**

```python
"""Sub-project 5a, Task 1 -- the entry lever for process_inbox_request.

Tests call the dispatcher directly. That is not a testing contrivance: it is
production's own DEBUG branch, app/activitypub/routes.py:758-759

    if current_app.debug:
        process_inbox_request(request_json, store_ap_json)
    else:
        process_inbox_request.delay(request_json, store_ap_json)

so the direct call is the task's body, reached the same way a DEBUG
deployment reaches it. Task 8's seam tests drive the same arms through a real
signed POST, which is what licenses any claim that the gate reaches them.
"""
import pytest

from app.models import ActivityPubLog
from tests.factories import inbox_activity, make_instance, make_site, make_user


def dispatch(activity, store_ap_json=True):
    """Call the dispatcher the way production's DEBUG branch does."""
    from app.activitypub.routes import process_inbox_request
    process_inbox_request(activity, store_ap_json)


def test_the_dispatcher_runs_against_seeded_rows_and_its_log_row_is_visible(
        app, db_session, monkeypatch):
    """The whole sub-project rests on three things holding at once: a row this
    test commits is visible to the dispatcher's independent task session, the
    dispatcher runs to completion without a request context, and a row IT
    writes is visible to this test after `finally: session.close()`.

    An unknown actor is the cheapest activity that reaches log_incoming_ap and
    returns: routes.py:869-870, 'Actor was not a user, feed or a community'.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')

    activity = inbox_activity(actor, activity_type='Announce')
    activity['actor'] = 'https://peer.example/u/nobody'

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == \
        'Actor was not a user, feed or a community'
```

- [ ] **Step 3: Run it**

```bash
./run_tests.sh tests/test_inbox_dispatch_preamble.py -v
```

Expected: PASS.

**STOP CONDITION.** If it fails, do not reach for a contrivance — do not patch
`get_task_session`, do not fake the session, do not assert on something else
because the log row is unreachable. Record the exact failure, what it proves
about the harness, and stop the sub-project for a decision. Sub-project 4 had the
same stop condition on its signing lever and it is what kept that work honest.

- [ ] **Step 4: Record the request-context asymmetry in the module docstring**

Append to the docstring written in Step 2 what Step 1's question 2 established:
under a direct call `patch_db_session` patches, so the dispatcher's `session` and
`db.session` are the same object; under Task 8's seam tests it does not, so the
dispatcher's `session` local is the task session while `db.session` remains the
request session. Name it as a real behavioural difference between two production
paths, not as a test detail — Task 9 decides whether it is a finding.

- [ ] **Step 5: Commit**

```bash
git add tests/test_inbox_dispatch_preamble.py
git commit -m "test: prove the inbox dispatcher can be entered directly

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The preamble's actor resolution for Announce, Accept and Reject

**Files:**
- Modify: `tests/test_inbox_dispatch_preamble.py`

**Interfaces:**
- Consumes: `dispatch` from Task 1.

`routes.py:861-870`. For `Announce`/`Accept`/`Reject` the actor is looked up as a
community, then a feed, then a user, each with `create_if_not_found=False`, and
all three missing is a logged refusal.

- [ ] **Step 1: Derive the outcome table from source, and expect it to be wrong somewhere**

Read `routes.py:861-870` and write the table into the file as a comment: which
lookup runs, in what order, what short-circuits it, and what each outcome logs.
Every enumeration in this campaign has been wrong somewhere on re-derivation —
sub-project 3 had three. Derive it; do not copy this plan's prose.

- [ ] **Step 2: Write the four tests**

One test per resolution outcome. Each seeds exactly one actor kind so the
lookup order is what decides:

```python
def test_an_announce_from_a_known_community_resolves_it_as_the_community(
        app, db_session, monkeypatch):
    """routes.py:862 -- the first lookup wins, and the feed and user lookups
    below it never run. Asserted through the outcome the community path
    produces, not by counting calls."""

def test_an_announce_from_a_feed_falls_through_to_the_feed_lookup(
        app, db_session, monkeypatch):
    """routes.py:863-865 -- community_only finds nothing, feed_only does."""

def test_an_announce_from_a_user_falls_through_to_the_user_lookup(
        app, db_session, monkeypatch):
    """routes.py:865-866 -- both narrowed lookups miss, the wide one hits."""

def test_an_announce_from_an_unknown_actor_is_refused(
        app, db_session, monkeypatch):
    """routes.py:867-870 -- all three miss.

    Already written as Task 1's spike; move it here and keep one copy."""
```

- [ ] **Step 3: Prove `create_if_not_found=False` is load-bearing**

Drop it from `routes.py:862` and run the file. A test must fail. If none does,
the tests are asserting on a resolution that would have happened anyway and the
whole lookup chain is untested — widen the data until one does. Restore the
source afterwards and record the mutant and its killer in the module docstring.

- [ ] **Step 4: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_preamble.py -q && \
  git add tests/test_inbox_dispatch_preamble.py && \
  git commit -m "test: cover the dispatcher's Announce/Accept/Reject actor lookup

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The preamble's other branch, and a membership test that is not a type test

**Files:**
- Modify: `tests/test_inbox_dispatch_preamble.py`

**Interfaces:**
- Consumes: `dispatch` from Task 1.

`routes.py:871-892`. Everything that is not Announce/Accept/Reject resolves one
actor **with creation enabled**, then splits on its type.

- [ ] **Step 1: Cover the User and Community outcomes**

Five tests, one per arm of `routes.py:872-892`:

```python
def test_an_ordinary_activity_from_a_known_user_sets_user(app, db_session, monkeypatch):
    """routes.py:872-873."""

def test_add_from_a_group_actor_is_ignored_as_nodebb_topic_management(app, db_session, monkeypatch):
    """routes.py:875-877. 'Remove' takes the same arm; parametrise both."""

def test_update_group_from_a_group_actor_is_processed_as_a_community_update(app, db_session, monkeypatch):
    """routes.py:878-881."""

def test_update_orderedcollection_from_a_group_actor_is_ignored(app, db_session, monkeypatch):
    """routes.py:882-884, 'Follower count update from a.gup.pe'."""

def test_any_other_update_from_a_group_actor_is_refused(app, db_session, monkeypatch):
    """routes.py:885-887, 'Unexpected Update activity from Group'."""

def test_an_activity_from_an_actor_that_is_neither_is_refused(app, db_session, monkeypatch):
    """routes.py:890-892, 'Actor was not a user or a community'."""
```

- [ ] **Step 2: Probe the membership test with a string**

`routes.py:878` reads:

```python
elif request_json['type'] == 'Update' and 'type' in request_json['object']:
```

If `object` is a **string**, `'type' in <str>` is a legal substring test. A
string containing the letters "type" passes the guard, and `request_json['object']['type']`
on the next line then indexes a string with a string.

```python
def test_an_update_from_a_group_actor_whose_object_is_a_string_containing_type(
        app, db_session, monkeypatch):
    """routes.py:878 -- `'type' in request_json['object']` is a membership
    test, and a membership test is never a type test. The findings doc states
    this rule in one line; it explains D13's original miss and D30's surviving
    mutant. This test establishes what the code ACTUALLY does with
    `object='https://peer.example/some-type-of-thing'` -- it does not assert
    a fix.

    Write the assertion from the observed behaviour, and record which it was:
    a TypeError escaping through routes.py:1885's `except`, or a silent fall
    to one of the refusals below.
    """
```

Run it, observe, then write the assertion to match. Note the outcome in the
docstring for Task 9 to register.

- [ ] **Step 3: Probe the dict actor**

`routes.py:856-857` does `actor_id = actor_id['id']` on a dict actor, unguarded —
the shape D47 registered one function away.

```python
def test_a_dict_actor_without_an_id_key(app, db_session, monkeypatch):
    """routes.py:856-857 -- Discourse sends a dict actor. A dict WITHOUT 'id'
    raises KeyError here, before any refusal can log. Establish the observed
    behaviour and assert it; do not fix it."""
```

- [ ] **Step 4: Drop each half of the compound guards**

For `routes.py:872` (`if actor and isinstance(actor, User)`) and `:874`, drop
each half separately. Each drop must be killed by a **different** test than the
other half's. If one test kills both, the data is too narrow: `actor` being
falsy and `actor` being a Community are different domains and need different
rows. Record all four mutants and their killers.

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_preamble.py -q && \
  git add tests/test_inbox_dispatch_preamble.py && \
  git commit -m "test: cover the dispatcher's Group branch, and probe its membership test

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: The Announce unwrap, including its self-recursion

**Files:**
- Create: `tests/test_inbox_dispatch_announce.py`

**Interfaces:**
- Consumes: `dispatch` from Task 1 (`from tests.test_inbox_dispatch_preamble import dispatch`).

`routes.py:894-933`. Six outcomes, two of which re-enter the function.

- [ ] **Step 1: Cover the string object**

```python
def test_an_announce_of_a_bare_uri_delegates_to_process_announce_of_uri(
        app, db_session, monkeypatch):
    """routes.py:896-900. The delegate logs its own outcome on every path
    (its docstring says so), so this test asserts the call and its arguments
    with a recorder, not a log row."""
```

- [ ] **Step 2: Cover both recursive arms — with the recursion running for real**

```python
def test_an_announce_of_a_list_processes_every_element(app, db_session, monkeypatch):
    """routes.py:901-907. Each element is rebuilt into its own single-object
    Announce and passed back into process_inbox_request.

    The recursion RUNS. Doubling `process_inbox_request` here would mean
    doubling the function under test, which produces a green test proving
    nothing. Two elements, both resolvable, and the assertion is that both
    were processed -- through two ActivityPubLog rows, one per element.
    """

def test_an_announce_of_an_ordered_collection_processes_every_item(
        app, db_session, monkeypatch):
    """routes.py:908-913, the same shape over `orderedItems`."""
```

- [ ] **Step 3: Probe the unguarded reads**

```python
def test_an_ordered_collection_without_ordered_items(app, db_session, monkeypatch):
    """routes.py:911 -- `request_json['object']['orderedItems']` is reached
    on `type == 'OrderedCollection'` alone. Establish and assert the observed
    behaviour for an object that has no such key."""

def test_an_announce_whose_inner_object_has_no_actor(app, db_session, monkeypatch):
    """routes.py:915 -- `request_json['object']['actor']`, unguarded, on a
    peer-supplied inner object."""
```

- [ ] **Step 4: Cover the inner-actor walk and what it sets**

```python
def test_an_announce_whose_inner_actor_is_banned_is_refused(app, db_session, monkeypatch):
    """routes.py:916-919."""

def test_an_announce_whose_inner_actor_is_unfound_is_refused(app, db_session, monkeypatch):
    """routes.py:920-922."""

def test_an_announce_from_a_feed_skips_the_inner_actor_walk_and_clears_user(
        app, db_session, monkeypatch):
    """routes.py:914 and :923-924 -- `if not feed:` guards the whole walk, and
    the feed path sets `user = None` instead. Assert through an inner object
    whose actor WOULD be refused if the walk ran: reaching the arm below
    proves it did not."""

def test_an_announce_sets_core_activity_to_the_inner_object(app, db_session, monkeypatch):
    """routes.py:928-930 -- `announced = True` and `core_activity` becomes the
    inner object, which is what every arm downstream dispatches on. Assert it
    by sending an Announce of a Like and observing the Like arm's outcome:
    without this assignment the dispatcher would branch on 'Announce' and
    fall through to the bottom."""
```

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_announce.py -q && \
  git add tests/test_inbox_dispatch_announce.py && \
  git commit -m "test: cover the dispatcher's Announce unwrap and its recursion

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: The vote arms, `process_upvote` and `process_downvote`

**Files:**
- Create: `tests/test_inbox_dispatch_votes.py`

**Interfaces:**
- Consumes: `dispatch` from Task 1.

Arms at `routes.py:1328-1334`; delegates at `:2387-2410` and `:2413-2433`.

- [ ] **Step 1: Cover both arms' dispatch**

`Like` and `EmojiReact` share one arm; `Dislike` has its own. Parametrise the
two types that reach `process_upvote` and assert the delegate and its four
arguments with a recorder.

- [ ] **Step 2: Cover `process_upvote` end to end**

Tests for: the unfound object refusal (`:2400-2402`), the successful vote
(`:2405-2407`), the announced/not-announced `ap_id` split (`:2390`), the emoji
split (`:2391-2394`), the dict `ap_id` unwrap (`:2397-2398`), the
`announce_activity_to_followers` call on the not-announced path only (`:2408`),
and the `Cannot upvote this` refusal (`:2410`).

- [ ] **Step 3: Pin the asymmetry between the two delegates**

`process_downvote`'s inner `if` has an `else` that logs `'Cannot downvote this'`
(`:2431`). `process_upvote`'s inner `if` (`:2404-2408`) has **no `else`** — so an
upvote blocked by `blocked_users` or `VOTE_QUOTA` logs nothing at all, where the
identical downvote logs `IGNORED`.

```python
def test_an_upvote_blocked_by_the_vote_quota_logs_nothing(app, db_session, monkeypatch):
    """process_upvote, routes.py:2404-2408 -- there is no `else` on the inner
    `if`, so this path is silent. The equivalent downvote (routes.py:2431)
    logs IGNORED. Asserted as `ActivityPubLog.query.count() == 0` WITH logging
    enabled, which is the assertion that would fail if a log call were added.

    Registered by Task 9 as an asymmetry, not fixed here."""

def test_a_downvote_blocked_by_the_vote_quota_logs_ignored(app, db_session, monkeypatch):
    """routes.py:2431, the same input through the other delegate."""
```

- [ ] **Step 4: Drop each half of the vote guards**

`can_upvote(user, liked.community) and not instance_banned(user.instance.domain)`
and the three-way inner conjunction. Each half dropped separately, each killed by
a distinct test. A banned instance, a blocked author and an over-quota voter are
three different domains and need three different rows.

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_votes.py -q && \
  git add tests/test_inbox_dispatch_votes.py && \
  git commit -m "test: cover the dispatcher's vote arms and their two delegates

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `process_poll_vote` and `process_question_answer`

**Files:**
- Modify: `tests/test_inbox_dispatch_votes.py`

- [ ] **Step 1: Confirm which Redis double applies, before writing anything**

`process_question_answer` (`routes.py:2475`) does `from app import redis_client`
**inside the function**, then `redis_client.lock(...)`. `tests/conftest.py:394`'s
`redis_double` docstring is explicit that this is the case it covers — the import
is re-executed per call, so patching the single `app.redis_client` attribute
redirects it — while `get_redis_connection` needs each of its four import sites
patched separately. Confirm by reading, and record which mechanism this delegate
uses in the test's docstring. The findings doc has already had to correct a stale
claim about this fixture once.

- [ ] **Step 2: Cover `process_poll_vote`**

Tests for: the unfound post refusal (`:2445-2447`), the successful vote
(`:2453-2456`), the unfound-choice refusal (`:2458`), the `instance_banned`
refusal (`:2460`), the announced/not-announced split on both `ap_id` and
`choice_text` (`:2439-2440`), and the dict `ap_id` unwrap.

Add a probe for the unguarded read:

```python
def test_a_poll_vote_without_choice_text(app, db_session, monkeypatch):
    """routes.py:2440 -- `request_json['choice_text']` is read unguarded from
    a peer-supplied activity. Establish and assert the observed behaviour."""
```

- [ ] **Step 3: Cover `process_question_answer`**

Tests for: the unfound reply refusal (`:2470-2472`), the success path including
the `Notification` row and the `unread_notifications` increment (`:2477-2492`),
the local-author-only guard on the notification (`:2478`), the not-announced
`announce_activity_to_followers` call (`:2494-2495`), and the `Cannot set answer`
refusal (`:2496`).

- [ ] **Step 4: Drop each half of the permission triple**

`post_reply.user_id == post_reply.post.user_id or post_reply.community.is_moderator(user) or post_reply.author.is_instance_admin()`
is three alternatives plus an `instance_banned` conjunct. Drop each separately;
each needs a distinct killer. Three alternatives that one row satisfies are three
untested alternatives.

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_votes.py -q && \
  git add tests/test_inbox_dispatch_votes.py && \
  git commit -m "test: cover the poll-vote and choose-answer delegates

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Flag, Move, QuoteRequest, and the `except`/`finally`

**Files:**
- Create: `tests/test_inbox_dispatch_misc.py`

- [ ] **Step 1: Cover Flag**

`routes.py:1344-1354`. Both arms: the reported object found (delegate called,
`SUCCESS` logged, `announce_activity_to_followers` called with `is_flag=True` and
`admin_instance_id`), and not found (`'Report ignored due to missing content'`).
`process_report` is doubled and asserted, not entered.

- [ ] **Step 2: Cover Move**

`routes.py:1571-1588`. This arm has a permission check, so it gets the same
treatment as any guard:

```python
def test_a_move_by_the_post_author_moves_the_post(app, db_session, monkeypatch):
    """routes.py:1579-1588."""

def test_a_move_by_a_moderator_of_the_origin_community_moves_the_post(app, db_session, monkeypatch):
    """routes.py:1579, second alternative."""

def test_a_move_by_an_instance_admin_of_the_origin_instance_moves_the_post(app, db_session, monkeypatch):
    """routes.py:1579, third alternative -- note it requires BOTH
    `origin_community.instance_id == user.instance_id` AND `is_instance_admin`."""

def test_a_move_by_an_unrelated_user_does_nothing(app, db_session, monkeypatch):
    """routes.py:1579 with every alternative false. Note what this arm does
    NOT do: there is no else, so no refusal is logged. Assert
    `ActivityPubLog.query.count() == 0` with logging enabled, and assert the
    post did not move."""

def test_a_move_whose_post_is_unknown_locally_is_resolved_remotely(app, db_session, monkeypatch):
    """routes.py:1575-1577 -- `resolve_remote_post_from_search` is doubled and
    asserted (sub-project 3 covered it); assert the '/context' strip."""
```

Drop each of the three permission alternatives separately; three distinct killers.

- [ ] **Step 3: Cover QuoteRequest, and probe its unguarded read**

```python
def test_a_quote_request_delegates_and_logs_success(app, db_session, monkeypatch):
    """routes.py:1880-1884."""

def test_a_quote_request_without_an_instrument(app, db_session, monkeypatch):
    """routes.py:1882 -- `core_activity['instrument']['id']` is read unguarded
    from a peer-supplied activity, the same KeyError shape as D2 and D13.
    Establish and assert the observed behaviour."""
```

- [ ] **Step 4: Cover the `except`/`finally`**

```python
def test_an_exception_inside_the_dispatcher_rolls_back_and_re_raises(
        app, db_session, monkeypatch):
    """routes.py:1885-1887 -- `except Exception: session.rollback(); raise`.
    The re-raise is the point: this is a Celery task, and swallowing here
    would lose the activity silently. Drive it by making a doubled delegate
    raise, and assert the exception propagates."""

def test_the_session_is_closed_even_when_an_arm_raises(app, db_session, monkeypatch):
    """routes.py:1888-1889 -- `finally: session.close()`. Assert on the
    session object itself, captured by patching get_task_session to record
    the session it returns while still returning a real one."""
```

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_misc.py -q && \
  git add tests/test_inbox_dispatch_misc.py && \
  git commit -m "test: cover the dispatcher's Flag, Move and QuoteRequest arms

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: The seam — three signed requests that reach these arms through the gate

**Files:**
- Modify: `tests/test_inbox_dispatch_preamble.py`

Everything above enters the dispatcher directly. That is a real production path,
but it does not by itself prove a remote peer can reach these arms. This task
proves it, for three shapes, and no more than three.

- [ ] **Step 1: Write the three seam tests**

```python
def test_a_signed_like_reaches_the_upvote_arm_through_the_gate(
        app, client, db_session, signing_peer, monkeypatch):
    """The full path: a REAL signed POST to /inbox, through every gate check
    sub-project 4 covered, into process_inbox_request, out at the Like arm.

    DEBUG=True makes the gate call the dispatcher inline (routes.py:758-759)
    rather than queueing it, which is what lets one request produce an
    assertion about an arm.
    """
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    ...
    response = signed_inbox_post(client, activity, signing_peer)
    assert response.status_code == 200

def test_a_signed_announce_reaches_the_unwrap_through_the_gate(
        app, client, db_session, signing_peer, monkeypatch):
    """The same, for the Announce unwrap -- the other preamble path."""

def test_the_actor_the_gate_verified_is_the_actor_the_arm_receives(
        app, client, db_session, signing_peer, monkeypatch):
    """The seam's real content. The gate verifies a signature against one
    actor and the dispatcher re-resolves `request_json['actor']`
    independently; this pins that they agree, which is the assumption every
    arm makes when it trusts `user`."""
```

- [ ] **Step 2: Record what the seam does and does not license**

In the module docstring, state exactly this: the gate reaches the preamble, the
Announce unwrap and the vote arms, for these three shapes. It does **not** show
anything about Follow, Accept, Reject, Create/Update, the moderation arms or
Undo, which slices 5b–5d cover. Task 9 uses this wording; overstating it is the
error the campaign keeps correcting.

- [ ] **Step 3: Note the request-context difference these tests run under**

Task 1 Step 4 established that `patch_db_session` does **not** patch inside a
request context. These three tests are the only ones in the sub-project that run
the dispatcher that way. State it in their docstrings, and flag for Task 9 that
the dispatcher therefore runs against two different session arrangements
depending on whether it was reached inline or through the queue.

- [ ] **Step 4: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_preamble.py -q && \
  git add tests/test_inbox_dispatch_preamble.py && \
  git commit -m "test: reach the dispatcher's arms through a real signed request

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Whole-unit confirmation and the register

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**Report only. Fix nothing.**

- [ ] **Step 1: Measure every unit in scope, whole**

```bash
./run_tests.sh tests/test_inbox_dispatch_preamble.py tests/test_inbox_dispatch_announce.py \
  tests/test_inbox_dispatch_votes.py tests/test_inbox_dispatch_misc.py \
  -q --cov=app.activitypub.routes --cov-report=json
```

Read `executed_lines` and `missing_branches` against each of the ten spans in the
spec's scope table. Report the figures and **explain every gap**. An unexplained
remainder is not an acceptable outcome for this task.

- [ ] **Step 2: File what the tests found**

At minimum, each with severity argued from what the tests executed rather than
from reading: the dict actor without `id` (`:857`), the membership-not-type test
(`:878`), `orderedItems` unguarded (`:911`), the inner `actor` unguarded (`:915`),
the shipped `s.rimu.geek.nz` breakpoint hook (`:859`), the unbounded list
recursion (`:901-907`), `choice_text` unguarded (`:2440`), `instrument` unguarded
(`:1882`), the upvote/downvote logging asymmetry (`:2404-2408` against `:2431`),
Move's silent no-op when every permission alternative is false, and the
session-arrangement difference between the inline and queued paths.

The campaign's next free number is **D49** — take numbers in order and update the
allocation ledger in the same commit.

- [ ] **Step 3: Convert what the seam actually proved, and nothing more**

D21, D24 and D35 are reading-only for reachability. This sub-project executed the
dispatcher, but through the gate only for three shapes. Say which of those three
findings the seam reaches and which it does not, and upgrade only what the tests
support. Sub-project 4 had to write the same paragraph and it is the campaign's
most-repeated correction.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -m "docs: register the inbox dispatcher's findings

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: The floor, and the lever documented

**Files:**
- Modify: `coverage_floors.ini`, `tests/README.md`

- [ ] **Step 1: Measure the full suite**

```bash
./run_tests.sh -q --cov=app.activitypub.routes --cov-report=json
```

The whole suite, not this sub-project's files: other tests touch this module and
the floor is a property of the module under the full suite.

- [ ] **Step 2: Raise the floor**

Set `app/activitypub/routes.py` in `coverage_floors.ini` to one point below the
measured blended figure, as sub-project 4 set 14 against 14.76. Record the
measured figure in the commit message so the next sub-project can see what the
floor was set against.

- [ ] **Step 3: Document the entry lever in `tests/README.md`**

A short section: how to enter `process_inbox_request` directly, that this is
production's DEBUG branch and not a contrivance, that seeded rows reach the task
session because `db_session` truncates rather than rolls back, and that
`patch_db_session` does not patch inside a request context — so the direct path
and the inline-through-the-gate path run under different session arrangements.
Slices 5b–5d will each need this and should not re-derive it.

- [ ] **Step 4: Run the ratchet and commit**

```bash
./run_tests.sh -q && \
  git add coverage_floors.ini tests/README.md && \
  git commit -m "docs: raise app/activitypub/routes.py's floor after the dispatcher work

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Plan self-review

**Spec coverage.** All ten spans in the spec's scope table map to tasks: the
preamble to Tasks 2–4, the vote arms and their four delegates to Tasks 5–6, Flag,
Move, QuoteRequest and the `except`/`finally` to Task 7. The spec's seam suite is
Task 8; its register requirement is Task 9; its floor commitment is Task 10. The
`redis_double` question the spec raised is Task 6 Step 1. The spike with a stop
condition the spec required is Task 1.

**Every finding the spec predicted has a task that probes it**, and each probe is
written to establish observed behaviour rather than to assert a fix — Tasks 3, 4,
6 and 7, registered in Task 9.

**Interface consistency.** `dispatch(activity, store_ap_json=True)` is defined
once, in Task 1, and Tasks 4–7 import it from
`tests.test_inbox_dispatch_preamble` by that exact name. `signed_inbox_post` and
`inbox_activity` keep the signatures `tests/factories.py` already gives them.

**Two things may prove uncoverable**, and both are handled by reporting rather
than contrivance: the string-object membership test (Task 3 Step 2) may raise
rather than fall through, and the `finally: session.close()` assertion (Task 7
Step 4) depends on being able to capture the task session without replacing it.
Neither is allowed to become a patched-out test.

**The stop condition is real.** If Task 1 fails, Tasks 2–10 are all blocked, and
the plan says to stop rather than to work around it.

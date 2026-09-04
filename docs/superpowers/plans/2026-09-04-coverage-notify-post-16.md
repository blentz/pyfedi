# Sub-project 16: the post-side create and notify path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover `create_post`, `notify_about_post` and `notify_about_post_task`
— 53 uncovered statements — fix the `NOTIF_FEED` arm's misplaced
de-duplication, and register the asymmetries the four arms carry.

**Architecture:** One new test file, `tests/test_ap_notify_post.py`, driving all
three functions by direct call. Tests first across six tasks; then one task
carrying the single production fix, and one recording the findings and adding
the register's family index.

**Tech Stack:** pytest, `./run_tests.sh` (podman-compose, tmpfs Postgres),
`tests/factories.py`.

**Spec:** `docs/superpowers/specs/2026-09-04-coverage-notify-post-16-design.md`

## Global Constraints

- Defects found in these three functions are fixed test-first, each in its own
  commit, separate from every test-only commit, and each proved by a mutation
  that fails a named test. Anything outside them is registered.
- **A defect is fixed when the correct spelling already exists in the file and
  the change is mechanical; it is registered when the fix would require
  choosing new behaviour for a case the codebase has never handled.** Before
  ruling register-only for want of a correct spelling, **grep the file for the
  twin** — and check the register for whether that twin is itself a D-entry,
  because copying a registered defect imports a broken guard.
- Findings are numbered from **D274**, and **all** live "Next free number"
  notes are updated in the same change. The historical notes are frozen
  records — leave them alone.
- The coverage floor for `app/activitypub/util.py` rises to the measured
  blended figure rounded down. It currently reads **71**.
- Locate every code target by content, not by the line numbers in this plan.
- **Verify a citation's function attribution separately from its line range**,
  and derive a function's extent from an **unfiltered** scan of `^def `.
  **Correcting a claim does not correct its copies** — grep for the wrong
  claim, and for the entities a correction names.
- Every test asserts on **persisted row state**, never merely that nothing
  raised.
- No vacuous assertions: never assert a value equal to a column's declared
  default without seeding a contrary baseline first.
- **Docstrings must be true.** Do not state a position, ordering, distance or
  count in prose unless you have just read it. Prefer quoting production code
  to describing it — a quoted block is the one kind of claim that can be
  audited mechanically.
- The full suite must pass. **Only the controller runs it**, one session at a
  time, and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## A note on test counts

**This plan states a per-task delta, never a running total.** Sub-project 13
stated running totals and corrected them five times; 14 and 15 used deltas and
corrected none. **The delta is an estimate, never a cap or a floor** — across
sub-projects 14 and 15, tasks needed more than their briefs predicted eleven
times, and every overage was justified by a surviving mutant.

## The harness facts that decide this plan's shape

**`notify_about_post_task` runs on `get_task_session()`, not `db.session`.**
That session has autoflush at SQLAlchemy's default `True`, where the app
factory configures `db.session` with `autoflush=False`. A commit inside the
task does **not** expire the test's objects, so **`db.session.refresh(obj)` is
required before asserting** on anything the task wrote. This is sub-project
13's harness and the opposite of what 14 and 15 needed.

**Coverage cannot see an unexercised ternary arm.** `tests/README.md` fact 87:
coverage.py emits no arc for a conditional expression, so a region at 100%
statements *and* 100% branches can still hide one. This region holds six.
**Task 6 exists solely because the coverage figure will not find them**, and
the criterion is stated separately in the spec for the same reason.

**`NOTIF_USER` is `0`.** `app/constants.py` numbers it zero, and the block
helpers short-circuit on a falsy `user_id` (`if user_id == 0: return []`).
Those two facts are unrelated — the helpers take a *recipient* id, never the
notification type — but a reader who conflates them will write a fixture that
proves nothing. The plan states it once so nobody rediscovers it as a bug.

## File structure

| File | Responsibility |
|---|---|
| `tests/test_ap_notify_post.py` | **Create.** Every test in this slice. Helpers at the top, then `create_post`, then the four notification arms in source order, then the ternaries. |
| `app/activitypub/util.py` | **Modify, Task 7 only.** One fix, one commit. No other task touches it. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Task 8.** Entries from D274, plus the family index. |
| `tests/README.md` | **Modify, Task 8.** Harness facts; currently numbered to 87. |
| `coverage_floors.ini` | **Modify, Task 8.** `app/activitypub/util.py`, 71 → measured. |

---

### Task 1: The harness, `create_post`, and the dispatcher

**Files:**
- Create: `tests/test_ap_notify_post.py`

**Interfaces:**
- Produces: `PEER`, `_seed_scenario()`, `_post_doc(**fields)`,
  `_subscribe(user, entity_id, type_)` helpers and the `ap_log` fixture, used
  by every later task.

**Read before writing:** `create_post`'s five uncovered statements are the
`local_only` guard's body and the tail `except Exception as ex:` block — the
visibility guard is already covered by an existing suite elsewhere. Find all
three by content. Also read `notify_about_post`, which is a two-line dispatcher
on `current_app.debug`.

**`log_incoming_ap` writes nothing unless `LOG_ACTIVITYPUB_TO_DB` is on**, and
`config.py` defaults it off. `create_post`'s guards all log and return `None`,
so without the log row they are indistinguishable from each other and from a
deleted guard. The `ap_log` fixture below turns it on — and **the assertion
must be on the exact message**, never a substring: sub-project 15 found two
mutants surviving substring assertions because a deleted guard let a *later*
guard fire and write a message sharing the substring.

- [ ] **Step 1: Write the file's header, helpers and fixture**

```python
"""`create_post`, `notify_about_post` and `notify_about_post_task` -- the
post-side create and notify path, and the mirror of the reply notification
fan-out sub-project 15 covered to zero.

Entry is a direct call. `notify_about_post_task` takes a post id; the `app`
fixture puts celery in eager mode, so calling the undecorated function is the
path a worker runs.

`notify_about_post_task` runs on `get_task_session()`, whose autoflush is at
SQLAlchemy's default True, unlike `db.session`, which the app factory
configures `autoflush=False`. The task commits on its own session, so a test
holding a row from `db.session` needs `db.session.refresh()` to see what the
task wrote. That is the opposite of what sub-projects 14 and 15 needed.

`log_incoming_ap` writes an `ActivityPubLog` row only when
`LOG_ACTIVITYPUB_TO_DB` is true, and config.py defaults it False. Every
`create_post` guard logs and returns None, so the tests that pin them turn it
on and assert the message EXACTLY -- a substring can match a different guard's
row.
"""
import pytest

from app import db
from app.activitypub.util import (create_post, notify_about_post,
                                  notify_about_post_task)
from app.constants import (NOTIF_COMMUNITY, NOTIF_FEED, NOTIF_TOPIC,
                           NOTIF_USER)
from app.models import ActivityPubLog, Notification, Post, Topic, User
from app.utils import utcnow
from tests.factories import (make_community, make_community_block,
                             make_feed, make_feed_item, make_instance,
                             make_instance_block, make_notification_subscription,
                             make_post, make_site, make_user, make_user_block)

PEER = 'peer.example'


@pytest.fixture
def ap_log(app):
    """Turn on the ActivityPubLog write so a `create_post` guard is attributable.

    Without it every guard returns None and creates nothing, which makes them
    indistinguishable from each other and from a deleted guard. The `app`
    fixture is session-scoped (tests/conftest.py), so the restore is
    load-bearing: a leaked True would change behaviour for every later test in
    the process.
    """
    app.config['LOG_ACTIVITYPUB_TO_DB'] = True
    yield
    app.config['LOG_ACTIVITYPUB_TO_DB'] = False


def _seed_scenario(local_only=False):
    """A local community owned by user 1, a remote author, and one Post.

    `make_community` hardcodes `instance_id=1` and `user_id=1`, so an instance
    and a user are seeded first to occupy those ids -- the pattern
    tests/test_inbox_dispatch_votes.py documents.
    """
    make_site()
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    community.local_only = local_only
    author = make_user(instance, 'author')
    post = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, post, author


def _post_doc(**fields):
    """A Create activity's envelope plus its `object`.

    `create_post` reads `request_json['id']` for the log and
    `request_json.get('object')` for the visibility check, so both levels
    matter.
    """
    obj = {'id': f'https://{PEER}/post/1', 'type': 'Page'}
    obj.update(fields)
    return {'id': f'https://{PEER}/activities/create/1',
            'type': 'Create',
            'object': obj}


def _subscribe(user, entity_id, type_):
    """A NotificationSubscription. The four arms key on different entity ids --
    an author's user id, a community id, a topic id and a feed id -- so the
    caller supplies both halves rather than the helper guessing.
    """
    return make_notification_subscription(user, entity_id, type_)
```

- [ ] **Step 2: Write the `create_post` and dispatcher tests**

Cover: a `local_only` community discarding the post, asserting the exact log
message, a `None` return and **no `Post` created beyond the seeded one**; the
tail `except` reached through a real failure from `Post.new` rather than an
injected exception — this plan counted **one** explicit `raise` in `Post.new`'s
body, on a domain an admin has blocked, so seed that; **read it yourself, report
the exact message you assert, and say so if you find more than one**, since a
plan's count is the kind of claim this campaign re-derives rather than inherits;
and `notify_about_post` dispatching inline under `current_app.debug`,
asserted by spying on `notify_about_post_task` rather than by its effects.

**The `local_only` seed must be contrary to the column's default** — read the
model rather than assuming.

- [ ] **Step 3: Mutation-test the guards**

The `local_only` gate, the visibility gate's `in ('followers', 'direct')`
membership, and the tail `except`. One at a time; restore
`app/activitypub/util.py` and confirm `git diff -- app/` is empty after **every
single mutation**.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_notify_post.py -q`
Expected: **4 tests added.**

```bash
git add tests/test_ap_notify_post.py
git commit -m "test: cover create_post's guards and the notify dispatcher"
```

---

### Task 2: The `NOTIF_USER` arm

**Files:**
- Modify: `tests/test_ap_notify_post.py`

**Interfaces:**
- Consumes: Task 1's helpers and the `ap_log` fixture.
- Produces: `_notifications_for(user)`, used by every later arm task.

The first of four near-identical arms. It notifies everyone subscribed to the
**author**, and its `if` carries four conditions: the recipient is not the
author, is not already in `notifications_sent_to`, has not blocked the
community, and has not blocked the instance.

**Read the arm from source and list its conditions before writing.** This
plan's table says `blocked_users` is absent from this arm — **verify that**, and
if the plan is wrong, say so and test what is there.

- [ ] **Step 1: Write `_notifications_for` and the arm's tests**

```python
def _notifications_for(user):
    """Every Notification row for one recipient, freshly read.

    `notify_about_post_task` commits on `get_task_session()`, which does not
    expire objects held by `db.session`, so a query is the reliable read --
    an attribute on a stale ORM instance is not.
    """
    return db.session.query(Notification).filter_by(user_id=user.id).all()
```

Cover: a subscriber notified, asserting the row's `notif_type`, `subtype`,
`url` and `targets`; the **author** not notified even when subscribed to
themselves; a subscriber who has blocked the community not notified; a
subscriber who has blocked the instance not notified; and the unread counter
incremented from a **seeded non-default** value.

**Every "not notified" test needs a seeded non-zero `Notification` count
elsewhere**, so "none created" is distinguishable from "none exist".

- [ ] **Step 2: Mutation-test each condition separately**

Four conditions, four kills. **A test that reaches a condition's False side
naturally cannot kill a mutant that forces it False** — with four arms of
near-identical `if` chains this is the dominant hazard here, so check each kill
is attributable to the condition it names.

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_notify_post.py -q`
Expected: **5 tests added.**

```bash
git add tests/test_ap_notify_post.py
git commit -m "test: cover the notify task's NOTIF_USER arm"
```

---

### Task 3: The `NOTIF_COMMUNITY` arm

**Files:**
- Modify: `tests/test_ap_notify_post.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 2's `_notifications_for`.

Subscribers to the **community**. Its `if` carries four conditions, and this
plan's table says the set differs from `NOTIF_USER`'s — `blocked_users` present,
`blocked_communities` absent. **Verify against source before writing**, and
report what you find; the asymmetry is the reason this slice exists and a plan
that got it backwards would send you chasing the wrong fixture.

Cover the same five shapes as Task 2, keyed on the community, plus the
condition this arm has that Task 2's lacks. Assert `notif_type`, `subtype` and
`targets` — the `targets` dict differs between arms and is what distinguishes
which arm fired.

**Mutation-test each condition separately**, then run and commit.

Run: `./run_tests.sh tests/test_ap_notify_post.py -q`
Expected: **5 tests added.**

```bash
git add tests/test_ap_notify_post.py
git commit -m "test: cover the notify task's NOTIF_COMMUNITY arm"
```

---

### Task 4: The `NOTIF_TOPIC` arm, and the conditional topic lookup

**Files:**
- Modify: `tests/test_ap_notify_post.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 2's `_notifications_for`.
- Produces: `_seed_topic(community, name='news')`, used by Task 5 if needed.

Subscribers to the community's **topic**. This arm carries all three block
checks where the first two each omitted one.

**There is no `Topic` factory** — grep `tests/factories.py` to confirm, then
seed one by hand and attach it to the community. `Topic` has `machine_name` and
`name` columns the arm's `targets` dict reads.

**One question this task must answer.** The subscriber query runs *before* the
`Topic` fetch is guarded:

```python
topic_send_notifs_to = notification_subscribers(post.community.topic_id, NOTIF_TOPIC)
if post.community.topic_id:
    topic = session.query(Topic).get(post.community.topic_id)
for notify_id in topic_send_notifs_to:
    ...
    'topic_name': topic.name,
```

If a community with **no** topic can produce a non-empty subscriber list, the
loop reads an unbound `topic` and raises `NameError` on a peer-reachable path.
**Establish this from source**: read `notification_subscribers` and work out
what its query returns for `entity_id = None`. Report the answer either way —
if it is reachable, say so and it becomes a fix candidate; if it is not, name
which of the campaign's five catalogued causes makes it unreachable and **do
not force a fixture to fake it**.

Cover the arm's five shapes plus its third block check, and mutation-test each
condition separately.

Run: `./run_tests.sh tests/test_ap_notify_post.py -q`
Expected: **6 tests added.**

```bash
git add tests/test_ap_notify_post.py
git commit -m "test: cover the notify task's NOTIF_TOPIC arm"
```

---

### Task 5: The `NOTIF_FEED` arm and the cross-arm de-duplication

**Files:**
- Modify: `tests/test_ap_notify_post.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 2's `_notifications_for`.

The last arm, and the one with the defect. It differs from the other three in
shape as well as content: it first queries every `Feed` joined to the post's
community through `FeedItem`, then loops over subscribers **per feed** — a
nested loop the other three do not have.

**`make_feed` and `make_feed_item` exist** in `tests/factories.py`; read them
before seeding.

**This task pins the defect; Task 7 fixes it.** In the other three arms
`notifications_sent_to.add(notify_id)` sits **inside** the `if`; here it sits
in the `for notify_id` body, so it runs even for a recipient the arm filtered
out. Write the pin as: a user subscribed to **two** feeds, filtered out of the
first — blocked instance is the cleanest lever — and assert they receive **no**
notification for the second either. That is the bug, and the test will be
inverted when the fix lands.

**Also cover the cross-arm de-duplication itself**: a user subscribed under two
different types receives exactly one notification, and the `targets` dict shows
which arm won. That test is what proves the set does its intended job, and
without it the fix in Task 7 has nothing to be measured against.

Mutation-test the arm's conditions and the outer `for feed` loop.

Run: `./run_tests.sh tests/test_ap_notify_post.py -q`
Expected: **6 tests added.**

```bash
git add tests/test_ap_notify_post.py
git commit -m "test: cover the notify task's NOTIF_FEED arm and its dedup set"
```

---

### Task 6: The six conditional expressions

**Files:**
- Modify: `tests/test_ap_notify_post.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 2's `_notifications_for`.

**This task exists because coverage cannot see these.** `tests/README.md` fact
87: coverage.py emits no arc for a conditional expression, so a region at 100%
statements and 100% branches can still hide an unexercised arm behind every
one. Sub-project 15 found six such arms this way and one of them guarded a real
`None`.

**Enumerate every `x if y else z` in the three functions yourself**, from
source. The spec counted six at writing — `saved_json = request_json if
store_ap_json else None`, `community.ap_id if community.ap_id else
community.name` once per arm, and `author.ap_id if author.ap_id else
author.user_name` in one arm — **but a spec's count is exactly the kind of claim
this campaign re-derives rather than inherits.** Report your count and any
disagreement.

For each, test **both** arms or give a stated reason. Most are
`x.ap_id if x.ap_id else x.name` and need a fixture whose actor is **local**,
since `make_community` and `make_user`'s local shape leave `ap_id` as `None`.
Assert on the persisted `targets` dict, which is where the resolved value lands.

**Mutation-prove each**: drop the arm's condition, confirm exactly the new test
dies. Record kill type and sole-or-multi.

Run: `./run_tests.sh tests/test_ap_notify_post.py -q`
Expected: **6 tests added**, one per conditional expression.

```bash
git add tests/test_ap_notify_post.py
git commit -m "test: pin both arms of every conditional in the notify path"
```

---

### Task 7: Fix the `NOTIF_FEED` arm's de-duplication

**Files:**
- Modify: `app/activitypub/util.py`
- Modify: `tests/test_ap_notify_post.py`

**Interfaces:**
- Consumes: Task 5's pin.

**The only task that changes production code. ONE FIX, ONE COMMIT.**

`notifications_sent_to.add(notify_id)` in the `NOTIF_FEED` arm sits one
indentation level out from where its three sibling arms put it — in the
`for notify_id` body rather than inside the `if`. So a recipient the arm
filtered out is recorded as notified, and the `notify_id not in
notifications_sent_to` check then skips them for every later feed.

The correct spelling appears three times in the same function. **Match it.**

- [ ] **Step 1: Invert Task 5's pin**, so it asserts the recipient **is**
  notified for the second feed. Assert the `Notification` row's `targets`
  names the second feed, not merely that a row exists — a fix that notified
  them for the wrong feed would pass a weaker assertion.

- [ ] **Step 2: Run it and watch it FAIL.** Quote the failure **verbatim** in
  your report.

- [ ] **Step 3: Apply the fix. Run. Expect green.**

- [ ] **Step 4: Mutation-test** by reverting the indentation alone, confirming
  the pin fails and everything else passes, then restore and confirm
  `git diff -- app/activitypub/util.py` shows only the intended change.

- [ ] **Step 5: Re-run the earlier arms' mutations.** **Adding or removing a
  guard can unkill an existing test** — sub-project 15's Fix C did exactly
  this, and the mutant it unkilled had been killed only through a defect the
  fix removed. Every test that depended on the old behaviour of
  `notifications_sent_to` must be re-checked, and any that no longer kills
  needs a replacement.

```bash
git add app/activitypub/util.py tests/test_ap_notify_post.py
git commit -m "fix: only mark a feed subscriber notified when they were"
```

- [ ] **Step 6: Audit the docstrings the fix falsified.** Every claim in the
  file that the feed arm marks recipients it did not notify is now false. Grep
  for `dedup`, `notifications_sent_to`, `feed`, `skipped`, `suppress` — then
  **read** every hit. **Correcting a claim does not correct its copies**: grep
  for the wrong claim, and for the entities your correction names.

---

### Task 8: Register the findings, add the family index, raise the floor

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: every earlier task's findings, via the SDD ledger.

**Write no tests and change no production code.**

- [ ] **Step 1: Read the ledger in full**, then the per-task reports, then the
spec. **Register what was found, not what was predicted.**

- [ ] **Step 2: Write the entries, numbered from D274.** Read several existing
entries first and match the house style. Each states the defect, where it lives
**by content with a line citation correct at your commit**, how it was found,
whether fixed or registered, and if registered, why not.

At minimum, and the ledger will have more: the `NOTIF_FEED` de-duplication fix
with its commit; the two filter-set omissions — `NOTIF_USER` without
`blocked_users`, `NOTIF_COMMUNITY` without `blocked_communities` — each with
the plausible reading **and** the case it leaves open; the conditional topic
lookup's reachability verdict from Task 4; the per-recipient commit inside each
loop, which leaves a partial fan-out that `session.rollback()` cannot undo; and
the unread counter incrementing in all four arms where the reply-side twin
recounts in one branch.

**The file's correction convention** — D233, D234, D243, D253, D257 — appends a
**marked** correction rather than rewriting. But note the distinction
sub-project 15 drew: that convention protects cells recording what was true *at
their own verification time*. **A cell written with a wrong pointer is simply
wrong and gets fixed in place.**

- [ ] **Step 3: Add the unguarded-peer-input family index.**

The family now spans thirteen entries across two sections — D236, D238, D245,
D246, D247, D255-D259, D264, D270-D272. **Verify that list against the register
rather than trusting it**; it is this plan's count, taken at writing.

Add **one new section** listing every member with its site, its status, and
whether a sibling copy is known elsewhere. It is a navigation aid: **no
existing entry is renumbered, moved or edited.** This campaign has twice
registered a defect whose sibling was already registered, which is what the
index is for.

- [ ] **Step 4: Update every live "Next free number" note.** Say in your report
how you told the live ones from the frozen.

- [ ] **Step 5: Add the harness facts to `tests/README.md`**, which currently
runs to **87**. Add only what is not already there; **say which candidates you
dropped and why** — a redundant fact in an 87-fact file costs a reader more
than a missing one. Candidates from this slice:

- **`notify_about_post_task` commits per recipient, inside each loop**, so a
  test asserting on a partial fan-out sees committed rows that no rollback will
  remove.
- **Four near-identical `if` chains in sequence make "a test that reaches a
  guard's False side naturally cannot kill a forced-False mutant" the dominant
  hazard**, not an edge case — and the `targets` dict is what tells you which
  arm fired.
- Anything else in the ledger's rulings that generalises.

- [ ] **Step 6: Raise the floor.** `coverage_floors.ini`,
`app/activitypub/util.py`: from **71** to the measured blended figure rounded
down. **The controller supplies that figure — do not run a coverage run.**

- [ ] **Step 7: Verify every citation.** Task 7 moved lines. Verify every
`app/*.py` citation against current source, and **verify function attributions
separately from line ranges** — this campaign has shipped false attributions
whose line numbers were correct four times, one written as a correction, one an
argument read off an adjacent call. Derive any function extent from an
**unfiltered** `^def ` scan.

- [ ] **Step 8: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 16's findings and raise the util.py floor"
```

---

## Self-review

**Spec coverage.** All three functions have tasks: `create_post` and
`notify_about_post` (Task 1), the four arms (Tasks 2-5), the conditional
expressions (Task 6). The spec's one authorised fix is Task 7; its register
work, family index and floor are Task 8. Every row of both asymmetry tables is
reached: the filter-set differences (Tasks 2-5, registered in 8), the
`NOTIF_FEED` placement (5, 7), the session difference (Task 1's docstring and
`_notifications_for`), the per-recipient commit (8), the unread counter (2-5,
8), the conditional topic lookup (4). The spec's nine criteria map to Tasks 1-5
(1, 2, 4), Task 6 (3), Task 7 (5), Task 8 (6, 7, 8) and the controller's final
run (9).

**Placeholder scan.** No "TBD", no "add appropriate error handling". **Five
steps deliberately say "read X and report what you find"** — Task 1's
`Post.new` raise, Task 2's and Task 3's filter-set verification, Task 4's
topic-lookup reachability, Task 6's ternary enumeration, Task 8's family-list
verification. Each names what to read, what to produce, and what to report if
the answer differs from the plan's. Task 4's is the sharpest: it explicitly
authorises **reporting unreachable and naming the cause** rather than forcing a
fixture.

**Type consistency.** `_seed_scenario(local_only=False)` returns
`(community, post, author)` and every task unpacks it that way.
`_post_doc(**fields)`, `_subscribe(user, entity_id, type_)` and
`_notifications_for(user)` are called with those signatures throughout.
`create_post` takes `(store_ap_json, community, request_json, user,
announce_id)`, `notify_about_post` takes `(post)`, and
`notify_about_post_task` takes `(post_id)` — each used with that arity.

**Two things this plan does that its predecessors did not.**

1. **Task 6 exists as its own task rather than as a step inside another.** Every
   prior slice measured criterion 2 with coverage and could not have caught an
   unexercised ternary arm; sub-project 15 discovered why. Giving it a task
   makes it a reviewable gate rather than a checklist item that a busy
   implementer folds into "and mutation-test the guards".
2. **Tasks 2 and 3 are told the plan's own filter table may be wrong and to
   verify it first.** The table is the reason the slice exists, and a plan that
   got it backwards would send an implementer chasing a fixture for a check
   that is not there. Sub-project 15's plan was wrong about a guard count and
   cost a whole extra task; this states the doubt up front.

---

> **ANNOTATION, 2026-09-04, appended after this document's last original line; nothing above is revised.** The `NOTIF_FEED` defect this plan describes as present at `:403-406` and `:479-483` was **fixed** on 2026-09-04 by commit `0489dc1d`, and Task 7 did **not** invert Task 5's pin as `:479-483` anticipates: the defect turned out to be latent rather than live — no conjunct of that arm's guard is feed-dependent — so no pin could distinguish the two spellings, and an equivalence argument plus a 70-mutation re-run against both code versions took that gate's place (Ruling J). Read **D275** in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` for what production does now; this document is left unrevised on purpose, because it is the record of what was planned and its being wrong about the defect's liveness is the evidence for that ruling.

# Sub-project 17: `update_post_from_activity`'s type tails — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `update_post_from_activity` to zero uncovered statements and zero
uncovered branches — 107 and 83 remain after sub-project 14 — fix the poll
block's unguarded vote loops, and register what the coverage work surfaces.

**Architecture:** One new test file, `tests/test_ap_update_post_tails.py`,
driving the function by direct call. Tests first across seven tasks, then one
task carrying the authorised fix, then one recording findings and raising the
floor.

**Tech Stack:** pytest, respx (`http_mock`), `./run_tests.sh` (podman-compose,
tmpfs Postgres), `tests/factories.py`.

**Spec:** `docs/superpowers/specs/2026-09-04-coverage-update-tails-17-design.md`

## Global Constraints

- Defects inside `update_post_from_activity` are fixed test-first, each in its
  own commit, separate from every test-only commit, and each proved by a
  mutation that fails a named test — **unless the defect is latent**, in which
  case the fix lands under an equivalence argument and a re-run of the
  accumulated mutation tables, and the task says so.
- **A defect is fixed when the correct spelling already exists in the file and
  the change is mechanical; it is registered when the fix would require choosing
  new behaviour for a case the codebase has never handled.** Before ruling
  register-only for want of a correct spelling, **grep the file for the twin** —
  and check the register for whether that twin is itself a D-entry.
- Findings are numbered from **D284**, and **all** live "Next free number" notes
  are updated in the same change. Sub-project 16 found **five** live notes where
  the prior count said four; re-run the mechanical test rather than inheriting a
  number.
- The coverage floor for `app/activitypub/util.py` rises to the measured blended
  figure rounded down. It currently reads **72**.
- Locate every code target by content, not by the line numbers in this plan.
- **Verify a citation's function attribution separately from its line range**,
  and derive a function's extent from an **unfiltered** scan of `^def `.
- **Read line numbers with `grep -n`, `awk` on `NR`, or a pipe through `cat -n`
  — never by counting lines out of a bare `sed -n 'A,Bp'`**, which prints no
  numbers and is off by one whenever the range opens on a blank line. Harness
  fact 95.
- **Docstrings must be true.** Do not state a position, ordering, distance or
  count in prose unless you have just read it. Prefer quoting production code to
  describing it. A citation that quotes N lines cites those N lines.
- Every test asserts on **persisted row state**, never merely that nothing raised.
- No vacuous assertions: never assert a value equal to a column's declared
  default without seeding a contrary baseline first.
- **Primary-key collisions make assertions silently vacuous.**
  `tests/conftest.py:143` truncates with `RESTART IDENTITY`, so ids restart at 1
  in every test. Any assertion on an id-valued field needs pairwise-distinct
  seeds and an explicit `assert len({...}) == n` guard. Harness fact 89.
- The full suite must pass. **Only the controller runs it**, one session at a
  time, and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## A note on test counts

**This plan states a per-task delta, never a running total.** Sub-project 13
stated running totals and corrected them five times; 14, 15 and 16 used deltas
and corrected none. **The delta is an estimate, never a cap or a floor** —
across sub-projects 14 to 16, tasks needed more than their briefs predicted more
than a dozen times, and every overage was justified by a surviving mutant.

## The harness facts that decide this plan's shape

**`update_post_from_activity` opens with `redis_client.lock`.** The shared
`redis_double` fixture cannot serve it — fakeredis without lupa has no Lua
scripting and redis-py's `Lock.release()` issues an `EVALSHA`. Sub-project 14's
file declares a local `_RedisLockOnlyDouble` whose `.lock()` is a
`contextlib.nullcontext()`, itself modelled on
`tests/test_inbox_dispatch_votes.py`. This file needs a third copy — see Task 1,
which makes that a decision rather than a reflex.

**The function commits on `db.session`, not `get_task_session()`.**
`expire_on_commit` is at SQLAlchemy's default `True` — the app factory overrides
only `autoflush`, at `app/__init__.py:81` — so a commit inside the function
expires the test's objects and the next attribute access re-loads them. **No
explicit refresh is needed here.** That is the opposite of sub-projects 13 and
16, and the same as 14 and 15.

**`http_mock` asserts every registered route was called**
(`tests/conftest.py:288-295`, `assert_all_called=True`). A test that registers
both `likes` and `dislikes` but exercises a path reaching only one **fails**.
Register exactly the routes the path under test will hit.

**Under this harness `make_image_sizes` executes rather than enqueues.**
`tests/conftest.py` sets `task_always_eager=True`, and `make_image_sizes`
(`app/activitypub/util.py:1724-1729`) calls `make_image_sizes_async` inline under
`current_app.debug` and `.apply_async` otherwise — both run the 152-statement
body. Two techniques already exist in this suite and **they are not
interchangeable**; Task 6 chooses between them on a difference this plan states
there.

**`Site.admins()` reads `g.admin_ids` when present** (`app/models.py:3995-4000`),
and otherwise joins `user_role` for `ROLE_ADMIN` **or** `User.id == 1`. Since
`_seed_post` seeds `community_owner` as user 1, **user 1 is an admin by that
`or_` clause without any role row**. Task 7 must establish this from source
before seeding, and must not assume a role row is required.

## File structure

| File | Responsibility |
|---|---|
| `tests/test_ap_update_post_tails.py` | **Create.** Every test in this slice. Helpers at the top, then the clusters in source order: `Video`, `Question`, `Event` + attachment dispatch, the url-change block, the suspicious-domain tail, then the conditional expressions. |
| `app/activitypub/util.py` | **Modify, Task 4 only.** The authorised fix, its own commit. No other task touches it. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Task 9.** Entries from D284, plus the two parked sub-project 16 residuals. |
| `tests/README.md` | **Modify, Task 9.** Harness facts; currently numbered to 95. |
| `tests/test_ap_notify_post.py` | **Modify, Task 9 only**, and only to add `post.instance_id` to the distinctness guard. |
| `coverage_floors.ini` | **Modify, Task 9.** `app/activitypub/util.py`, 72 → measured. |

---

### Task 1: The harness and the `type == 'Video'` cluster

**Files:**
- Create: `tests/test_ap_update_post_tails.py`

**Interfaces:**
- Produces: `PEER`, `_RedisLockOnlyDouble`, the `redis_lock_only_double`
  fixture, `_seed_post(software='lemmy')` returning a `Post`, and
  `_update(**fields)` returning `{'object': fields}` — used by every later task.

**Read before writing.** `tests/test_ap_update_pair.py:1-105` is sub-project
14's harness for the same function. Read it whole. Its `_seed_post`, `_update`,
`PEER` and `_RedisLockOnlyDouble` are the shapes to match, and its module
docstring records why each exists.

**One decision this task owns.** `_RedisLockOnlyDouble` would be this file's
**third** copy — `tests/test_inbox_dispatch_votes.py` has one and
`tests/test_ap_update_pair.py` has the second, which cites the first. Three
copies is where this campaign's own duplication family (D242/D262) says
something. **Read those two copies and the register's duplication entries, then
decide**: a third local copy for consistency with the established pattern, or a
promotion to `tests/conftest.py`. Argue it either way in your report. Default to
the local copy if the argument is close — promoting shared fixtures is a change
to infrastructure three files depend on, and this slice did not scope it.

**The cluster.** `app/activitypub/util.py:3273-3309`, `type == 'Video'` — 20
uncovered statements, 9 uncovered branches. Find it by content. It fetches
PeerTube's `likes` and `dislikes` collections to recover vote totals:

```python
            for endpoint in endpoints:
                if endpoint in request_json['object']:
                    try:
                        object_request = get_request(request_json['object'][endpoint], headers={'Accept': 'application/activity+json'})
                    except httpx.HTTPError:
                        time.sleep(3)
                        try:
                            object_request = get_request(request_json['object'][endpoint], headers={'Accept': 'application/activity+json'})
                        except httpx.HTTPError:
                            object_request = None
```

**Quote the block you actually read**, not this one — the plan's copy is
truncated at the line ends and is here to tell you what shape to expect.

- [ ] **Step 1: Write the file's header, helpers and fixture**

Model the header on `tests/test_ap_update_pair.py:1-28`. It must state, from
what you have just read: that entry is a direct call; that the function opens
with `redis_client.lock` and why `redis_double` cannot serve it; that the
function commits on `db.session` with `expire_on_commit` at its default `True`,
so no refresh is needed; and that `http_mock` asserts every registered route is
called, so a test registers only the routes its path reaches.

Helpers, matching sub-project 14's signatures exactly so a reader moving between
the two files is not surprised:

```python
PEER = 'peer.example'


class _RedisLockOnlyDouble:
    """`app.redis_client` stand-in covering only `.lock(...)` as a context
    manager. See this module's docstring for why the shared `redis_double`
    cannot serve it.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


def _seed_post(software='lemmy'):
    """A local community owned by user 1, a remote author on PEER, and one Post.

    `make_community` hardcodes `instance_id=1` and `user_id=1`, so an instance
    and a user are seeded first to occupy those ids -- the same pattern
    tests/test_inbox_dispatch_votes.py documents.
    """
    make_site()
    instance = make_instance(PEER, software=software)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    post = make_post(community, author, ap_id=f'https://{PEER}/objects/1')
    db.session.commit()
    return post


def _update(**fields):
    """An Update activity's `object`, with only the keys a test names.

    The function reads `request_json['object']` and nothing else, so the
    envelope carries no `type`, `actor` or `id`.
    """
    return {'object': fields}
```

- [ ] **Step 2: Write the `Video` cluster's tests**

Cover, each asserting on **persisted** `post` columns read back after the call:
both endpoints present and returning `totalItems`, asserting `post.up_votes`,
`post.down_votes`, `post.score` and `post.ranking` against a **seeded contrary
baseline** — read the model for each column's declared default first and seed
something else; only `likes` present; neither present; a non-200 response; a
200 whose body is not JSON, which takes the bare `except:` at `:3291-3293`; a
200 whose JSON lacks `totalItems`.

**The early return is load-bearing.** The block ends `db.session.commit()` /
`return` at `:3308-3309` with the comment "return now for PeerTube, otherwise
rest of this function breaks the post". At least one test must prove the return
happened — assert that something the code *after* it would have changed did
**not** change. Read past `:3309` to pick that witness, and name it in the
docstring.

**Do not write a test that exercises the retry yet.** It costs three real
seconds per traversal (`time.sleep(3)` at `:3283`). Step 3 handles it.

- [ ] **Step 3: The retry pair**

The retry is two `get_request` calls with a `time.sleep(3)` between them.
Neutralise the sleep — `monkeypatch.setattr('app.activitypub.util.time.sleep',
lambda _: None)` is the obvious spelling, but **verify how `time` is imported in
that module before writing it**, because the patch target depends on it.

Two tests: first call raises `httpx.HTTPError` and the retry succeeds; both
raise and `object_request` becomes `None`. **A test asserting the retry happened
must distinguish it from the first attempt succeeding** — assert the route was
called twice, using the `respx` route object's call count, not the outcome.

- [ ] **Step 4: Mutation-test the cluster**

Every conjunct and every branch condition in `:3273-3309`, one at a time.
Record per Ruling C: the mutation, the named test that died, assertion-kill or
crash-kill, sole or multi. Restore `app/activitypub/util.py` and confirm
`git diff -- app/` is empty **after every single mutation**.

- [ ] **Step 5: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_post_tails.py -q`
Expected: **9 tests added.**

```bash
git add tests/test_ap_update_post_tails.py
git commit -m "test: cover the update path's PeerTube vote-total fetch"
```

---

### Task 2: The poll block's routing and its totals path

**Files:**
- Modify: `tests/test_ap_update_post_tails.py`

**Interfaces:**
- Consumes: Task 1's `PEER`, `_seed_post`, `_update`, `redis_lock_only_double`.
- Produces: `_poll_update(*choices, end_time=None, mode_key='oneOf')`, a helper
  building a `Question` object's vote list — used by Tasks 3 and 4.

**The cluster.** `app/activitypub/util.py:3311-3360`, `type == 'Question'` —
38 uncovered statements, 25 uncovered branches across this task and Task 3.
This task takes the routing (`:3311-3331`) and the totals path
(`:3353-3360`); Task 3 takes the Edit path.

`make_poll` and `make_poll_choice` exist in `tests/factories.py` — read them
before seeding. `Poll.post_id` is the primary key, so a post has at most one
poll. `PollChoice` carries `choice_text`, `sort_order` and `num_votes`
(`num_votes` has `default=0`, so assert against a seeded non-zero baseline).

- [ ] **Step 1: Write the helper**

```python
def _poll_update(*choices, end_time=None, mode_key='oneOf'):
    """A `Question` object carrying `choices` as its vote list.

    Each choice is a dict exactly as a peer would send it, so a test can pass a
    malformed one -- `{}`, or a dict with `name` but no `replies` -- without the
    helper repairing it. `mode_key` selects `oneOf` (mode 'single') or `anyOf`
    (mode 'multiple'); the production code reads one or the other and returns
    early when neither is present.
    """
    obj = {'type': 'Question', mode_key: list(choices)}
    if end_time is not None:
        obj['endTime'] = end_time
    return {'object': obj}


def _choice(name, total=None):
    """One entry in a Question's vote list.

    `total=None` omits `replies` entirely, which is what a peer sending a poll
    with no vote counts looks like; a number produces
    `{'replies': {'totalItems': n}}`.
    """
    vote = {'name': name}
    if total is not None:
        vote['replies'] = {'totalItems': total}
    return vote
```

- [ ] **Step 2: Routing tests**

Cover: `oneOf` present sets `mode` to `'single'`; `anyOf` present sets it to
`'multiple'`; neither present returns early with **nothing written** — assert a
seeded `Poll`'s columns are unchanged and the `PollChoice` rows still match what
was seeded.

**`mode` is only persisted on the Edit path** (`:3339`), so a routing test that
asserts `mode` must arrange for that path. Read `:3311-3351` and work out which
of your routing tests can assert `mode` at all; say so in the docstring for the
ones that cannot.

- [ ] **Step 3: The counting loop's three guards**

`:3323-3331` skips a vote missing `name`, missing `replies`, or whose `replies`
lacks `totalItems`. Each `continue` is a branch. Cover each **separately** — a
vote missing only that key, alongside one well-formed vote so the loop's other
iterations still run.

Assert on the **consequence**: a skipped vote contributes nothing to
`total_vote_count`, which decides whether `:3333` routes to the Edit path or the
totals path. So each guard test's observable is which path ran.

- [ ] **Step 4: The totals path**

`:3354-3358`: for each vote, look up the `PollChoice` by `choice_text` and set
`num_votes`. Cover: a matching choice is updated; a vote naming a choice that
does not exist leaves everything else correct (`:3356`'s `if choice:` false
arm); two choices updated in one Update.

Seed `num_votes` to a **non-zero, non-target** value first — the column's
declared default is 0, and asserting an update to a value equal to the default
would be vacuous.

- [ ] **Step 5: Mutation-test**

Every conjunct and branch in `:3311-3331` and `:3353-3358`, one at a time.
Record per Ruling C. **Do not mutate the unguarded reads at `:3355` and `:3357`
yet** — those are Task 4's, and mutating them here would record kills against a
defect a later task changes.

- [ ] **Step 6: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_post_tails.py -q`
Expected: **10 tests added.**

```bash
git add tests/test_ap_update_post_tails.py
git commit -m "test: cover the update path's poll routing and totals update"
```

---

### Task 3: The poll block's Edit path

**Files:**
- Modify: `tests/test_ap_update_post_tails.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 2's `_poll_update` and `_choice`.

**The path.** `app/activitypub/util.py:3333-3351`. It runs when
`total_vote_count == 0` — which, per the spec, is exactly the case where every
vote failed one of the counting loop's three guards. It is guarded by
`if poll:` (`:3335`) and by `if not 'endTime' in request_json['object']: return`
(`:3336-3337`), then sets `end_poll` and `mode`, issues two raw `DELETE`
statements, and recreates every choice.

```python
                    db.session.execute(text('DELETE FROM "poll_choice_vote" WHERE post_id = :post_id'),
                                       {'post_id': post.id})
                    db.session.execute(text('DELETE FROM "poll_choice" WHERE post_id = :post_id'), {'post_id': post.id})
```

**Quote what you read**, not this.

- [ ] **Step 1: Prove the DELETEs**

Seed **two** `PollChoice` rows and at least one `PollChoiceVote` row, then send
an Update whose votes all fail a counting guard. Assert the seeded rows are
**gone** and the new ones are present.

`tests/factories.py` has `make_poll_choice` but **no `PollChoiceVote` factory** —
grep to confirm, then construct the row directly. `PollChoiceVote`'s primary key
is `(choice_id, user_id)` and it also carries `post_id`; read the model before
seeding, and seed a `user` for it that is not user 1.

**A test that only asserts the new rows exist does not prove the DELETE ran** —
the recreate loop would produce the same final state by insertion alone if the
old rows had never existed. The seeded rows must be distinguishable from the
recreated ones: give them `choice_text` values the Update does not carry.

- [ ] **Step 2: The two guards**

Cover: no `Poll` row for the post, so `:3335`'s `if poll:` is false and the
function falls through to `return` at `:3351` without writing; `endTime` absent,
so `:3336-3337` returns **before** the DELETEs — assert the seeded choices
**survive**, which is the sharpest available witness that the early return
happened.

- [ ] **Step 3: `end_poll` and `mode`**

Assert both are persisted. `Poll.end_poll` is a `DateTime` column and `:3338`
assigns `request_json['object']['endTime']` **raw**. Establish what actually
lands in the column when a well-formed ISO-8601 string is sent, and assert that.

**If a malformed `endTime` raises or corrupts, that is a finding, not a test
failure** — record it for Task 9 with the exact exception, and do not repair it
here. Choosing a parse behaviour is new behaviour, which the fix rule sends to
the register.

- [ ] **Step 4: `sort_order`**

`:3345-3349` numbers choices from `i = 1`, incrementing per iteration. Assert
the persisted `sort_order` values for a three-choice Update. This assertion is
**what Task 4's fix will change**, so make it precise: assert the full
`(choice_text, sort_order)` set, not just that three rows exist.

- [ ] **Step 5: Mutation-test**

Every conjunct and branch in `:3333-3351`, one at a time, per Ruling C.
**Do not mutate `vote['name']` at `:3347`** — Task 4's.

- [ ] **Step 6: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_post_tails.py -q`
Expected: **7 tests added.**

```bash
git add tests/test_ap_update_post_tails.py
git commit -m "test: cover the update path's poll edit and choice recreation"
```

---

### Task 4: Fix the poll block's two unguarded vote loops

**Files:**
- Modify: `app/activitypub/util.py`
- Modify: `tests/test_ap_update_post_tails.py`

**Interfaces:**
- Consumes: Task 2's `_poll_update` and `_choice`, Task 3's `sort_order`
  assertion.

**The only task that changes production code. ONE DEFECT FAMILY, ONE COMMIT.**

Three loops iterate the same peer-supplied `votes` list. The counting loop
(`:3323-3331`) guards `'name'`, `'replies'` and `'replies']['totalItems']`. The
Edit-recreate loop (`:3346-3349`) reads `vote['name']` unguarded. The totals loop
(`:3354-3357`) reads `vote['name']` and `vote['replies']['totalItems']`
unguarded.

**Establish reachability from source before writing anything.** Both are
peer-reachable in principle; prove it by test, not by argument:

- Edit loop: every vote lacking `name` makes `total_vote_count == 0`, routing to
  the Edit path, which then reads `vote['name']`.
- Totals loop: one well-formed vote plus one malformed sibling makes
  `total_vote_count > 0` while leaving the malformed entry in the list.

**Sub-project 16's lesson applies:** a registered defect is not necessarily
observable. Confirm each raises before planning a fix around it.

- [ ] **Step 1: Write the two failing tests**

One per loop, each asserting the persisted end state a correct implementation
would produce — not merely that no exception escaped. Run them and **quote the
failures verbatim** in your report, with the exception type and the line.

- [ ] **Step 2: Decide the Edit loop's repair, and argue it**

The counting loop's spelling is `if not 'name' in vote: continue`. Applying it
to the Edit loop raises a question the counting loop does not have: `i`
increments on `:3349`, **inside** the loop body. So:

- `continue` **before** `i += 1` skips the malformed vote and leaves
  `sort_order` contiguous (1, 2, 3 for three well-formed votes among four).
- `continue` **after** `i += 1`, or incrementing regardless, leaves a gap
  (1, 2, 4).

**This is a behaviour choice wearing a mechanical fix's clothes.** Read how
`sort_order` is consumed — grep for `sort_order` across `app/` and the templates
— and let that decide. Write the argument in your report and in the fix's
commit message. If consumption makes both defensible, say so and pick the one
that matches how the same field is written elsewhere in `app/`.

- [ ] **Step 3: Apply the fix. Run. Expect green.**

Match the counting loop's spelling. Read all three loops and match; do not
invent a new guard shape.

- [ ] **Step 4: Mutation-test the fix**

Revert each added guard individually and confirm exactly the matching new test
fails and nothing else does. Then restore and confirm
`git diff -- app/activitypub/util.py` shows only the intended change.

- [ ] **Step 5: Re-run Tasks 1-3's mutations**

**Adding a guard can unkill an existing test.** Sub-project 15's Fix C did
exactly this, and the mutant it unkilled had been killed only through a defect
the fix removed. Re-run every mutation Tasks 1, 2 and 3 recorded, compare
against their tables, and state explicitly whether any was unkilled.

- [ ] **Step 6: Update Task 3's `sort_order` assertion** if your repair changed
  the numbering for a list containing a malformed vote. Say which assertion
  changed and why.

```bash
git add app/activitypub/util.py tests/test_ap_update_post_tails.py
git commit -m "fix: guard the poll edit and totals loops like the counting loop"
```

- [ ] **Step 7: Audit the docstrings the fix falsified.** Grep for `name`,
  `guard`, `counting loop`, `unguarded`, `totals` across
  `tests/test_ap_update_post_tails.py` — then **read** every hit. **Correcting a
  claim does not correct its copies**: grep for the wrong claim, and for the
  entities your correction names.

---

### Task 5: The `Event` block and the attachment dispatch

**Files:**
- Modify: `tests/test_ap_update_post_tails.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: `_seed_event_post()` returning a `(post, event)` pair, used by
  Task 6 if it needs an Event-typed post.

**Two clusters.** `app/activitypub/util.py:3364-3392` (Event, 5 uncovered
statements / 9 uncovered branches) and `:3411-3438` (the attachment dispatch's
`Document`, `Audio` and `Image` arms).

**There is no `Event` factory** — grep `tests/factories.py` to confirm, then
construct the row directly. `Event.post_id` is the primary key. The block reads
thirteen keys off `request_json['object']` unguarded (`:3367-3379`); a test that
omits one raises `KeyError`. **That is a register finding, not a fix** — see
Task 9 — so your fixtures supply all thirteen, and you record the omission
finding rather than repairing it.

**The Event block's image call is gated**: `:3388-3389` is
`if get_setting('cache_remote_images_locally', True): make_image_sizes(...)`.
Turning that setting off is the cheapest way to keep `make_image_sizes_async`
out of an Event test. `tests/test_event_post_type_survives_update.py:161`
documents the technique — read it.

- [ ] **Step 1: `_seed_event_post` and the Event tests**

Cover: an existing `Event` row updated across all thirteen fields, asserting
persisted values; `post.image` present so `:3380-3382` deletes it and records
`old_db_entry_to_delete`; `'image'` in the object so a new `File` is attached;
`'image'` absent so `:3391` sets `post.image_id = None`; no `Event` row for the
post, so `:3366`'s `if event:` is false.

Assert `File` rows by **count and identity**, and give the seeded and the new
`File` distinguishable `source_url` values — the ids collide otherwise
(`RESTART IDENTITY`).

- [ ] **Step 2: The attachment dispatch arms**

`:3416-3433` walks the attachment list, matching `Link` (`href`, then `url`),
`Document` (`url`), and `Audio` (`url`, plus `name` overwriting `post.title`).
`:3435-3438` is a second pass that takes an `Image` attachment's `url` only when
the first pass found nothing.

Cover each arm and each `break`: a `Document` attachment sets `new_url`; an
`Audio` attachment sets both `new_url` and `post.title`; an `Audio` attachment
without `name` leaves the title alone; an `Image`-only list reaches the second
pass; a list with both `Image` and `Link` takes the `Link` and **not** the
image — the comment at `:3434` says that is the point of the second pass being
conditional, so assert it.

**Every one of these tests changes `post.url`**, which routes into the
url-change block Task 6 owns. Keep the assertions here on `new_url`'s observable
consequence — `post.url` and `post.type` — and let Task 6 own the image and
opengraph machinery. **If a test here cannot avoid reaching `make_image_sizes`,
say so and apply Task 6's chosen technique rather than inventing one.**

- [ ] **Step 3: Mutation-test**

Every conjunct and branch in `:3364-3392` and `:3411-3438`, one at a time, per
Ruling C.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_post_tails.py -q`
Expected: **12 tests added.**

```bash
git add tests/test_ap_update_post_tails.py
git commit -m "test: cover the update path's Event block and attachment dispatch"
```

---

### Task 6: The url-change block, the image, and the opengraph fallback

**Files:**
- Modify: `tests/test_ap_update_post_tails.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 5's `_seed_event_post`.

**The cluster.** `app/activitypub/util.py:3464-3500` — the `if old_url !=
new_url:` arm. It deletes the old image, classifies the new url, builds a `File`
from an image url or from `request_json['object']['image']['url']` or from
opengraph, sets `post.type`, and calls `make_image_sizes`.

**This task owns the image-boundary decision for the whole file.** Under this
harness `make_image_sizes` executes (see *The harness facts* above). Two
techniques exist and **they are not interchangeable**:

1. **Serve a 404** so `make_image_sizes_async` stops at its `get_request`, whose
   bare `except:` swallows the failure.
   `tests/test_ap_actor_json_person.py:898-908` uses it and
   `tests/README.md:1091` records the bare-except behaviour. Read both.
2. **Turn off `cache_remote_images_locally`.**
   `tests/test_event_post_type_survives_update.py:161` uses it. **This guards
   only the Event block's call at `:3389`, which is gated on the setting. The
   call at `:3496` is NOT gated**, so technique 2 alone does not protect this
   task's path.

**Verify that difference from source before choosing** — read `:3388-3389` and
`:3492-3496` and confirm which is gated. State the choice and the reason in the
file's docstring, so Task 5 and any later reader inherit one technique rather
than two.

- [ ] **Step 1: The image-url path**

`:3472-3477`: an image url sets `POST_TYPE_IMAGE`, builds a `File`, and takes
`alt_text` from the first attachment's `name` when the attachment is a list
whose first entry has one. Cover: with `alt_text` available and without;
assert the persisted `File.alt_text` and `post.type`.

- [ ] **Step 2: The `object['image']['url']` path**

`:3479-3480`. Cover it, and cover the `else` that falls through to opengraph.

- [ ] **Step 3: The opengraph fallback**

`:3483-3487`. `opengraph_parse(thumbnail_url)` is a network call — serve it
through `http_mock`. Cover: opengraph returns an `og:image`; returns
`og:image:url` and no `og:image`; returns a filename starting with `/`, which
`:3486` rejects; returns nothing.

**`:3485` is `opengraph.get('og:image') or opengraph.get('og:image:url')`** —
read it and confirm the precedence before asserting which one wins.

- [ ] **Step 4: `post.type` classification and the `File` commit**

`:3488-3491` sets `POST_TYPE_VIDEO` for a video hosting site or video url, and
`POST_TYPE_LINK` otherwise. `:3492-3498` commits the `File` and attaches it, or
clears `old_db_entry_to_delete`. Cover both arms of each.

- [ ] **Step 5: Mutation-test**

Every conjunct and branch in `:3464-3500`, one at a time, per Ruling C.

- [ ] **Step 6: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_post_tails.py -q`
Expected: **11 tests added.**

```bash
git add tests/test_ap_update_post_tails.py
git commit -m "test: cover the update path's url change, image and opengraph fallback"
```

---

### Task 7: The suspicious-domain notification tail

**Files:**
- Modify: `tests/test_ap_update_post_tails.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 6's image-boundary technique.

**The cluster.** `app/activitypub/util.py:3501-3559` — 13 uncovered statements,
11 uncovered branches. When the url's domain changed, a banned-adjacent domain
with `notify_mods` or `notify_admins` set produces `Notification` rows, then
`post.domain` is updated and cross-posts recalculated.

**`Site.admins()` does not need a role row.** `app/models.py:3995-4000` reads
`g.admin_ids` when present and otherwise joins `user_role` for `ROLE_ADMIN`
**or** `User.id == 1`. `_seed_post` seeds `community_owner` as user 1.
**Verify this from source before seeding** and say in the docstring which arm
your fixture takes — if you set `g.admin_ids`, the join never runs, and a test
that believes it is exercising the role path would be proving nothing.

`make_domain` and `make_community_member(user, community, is_moderator=...)`
exist in `tests/factories.py`. `Domain.notify_mods`, `notify_admins`,
`post_count` and `banned` all default in the model — read them and seed contrary
baselines.

- [ ] **Step 1: The mod-notify arm**

`:3511-3519`. Seed a moderator, a domain with `notify_mods` true, and an Update
changing the url to that domain. Assert the persisted `Notification` row's
`title`, `url`, `user_id`, `author_id`, `notif_type`, `subtype` and the full
`targets` dict.

**`targets` carries `post_id`** — apply harness fact 89: arrange ids pairwise
distinct and guard with an explicit `assert len({...}) == n` before asserting,
or the assertion is mutable to a sibling id with the suite still green.

- [ ] **Step 2: The admin-notify arm and the de-duplication**

`:3520-3534`. Cover: `notify_admins` true produces a row for an admin; an admin
who is **also** a moderator already in `already_notified` gets **one** row, not
two — that is what `:3522`'s `if admin.id not in already_notified:` is for.

**`already_notified` holds `community_member.user_id` (`:3519`) and is tested
against `admin.id` (`:3522`).** Confirm from the models that those are the same
key space before relying on the de-duplication; if they are not, that is a
finding for Task 9.

- [ ] **Step 3: `post_count`, `post.domain`, and the cross-post arms**

`:3535-3536` increments `new_domain.post_count` and sets `post.domain`. Seed
`post_count` to a non-zero value first. `:3539-3540` and `:3558-3559` call
`calculate_cross_posts` on `if post.cross_posts is not None`; cover both arms of
both.

- [ ] **Step 4: The `else` arm**

`:3542-3559` runs when the Update carried no url: `POST_TYPE_ARTICLE`,
`post.url = None`, `post.image_id = None`. The comment at `:3544-3555` is a
prior sub-project's fix carrying its reasoning — **do not re-litigate it**;
cover the code and leave the comment alone.

- [ ] **Step 5: Mutation-test**

Every conjunct and branch in `:3501-3564`, one at a time, per Ruling C.

- [ ] **Step 6: Run and commit**

Run: `./run_tests.sh tests/test_ap_update_post_tails.py -q`
Expected: **10 tests added.**

```bash
git add tests/test_ap_update_post_tails.py
git commit -m "test: cover the update path's suspicious-domain notifications"
```

---

### Task 8: The conditional expressions

**Files:**
- Modify: `tests/test_ap_update_post_tails.py`

**Interfaces:**
- Consumes: every earlier task's helpers.

**This task exists because coverage cannot see these.** `tests/README.md` fact
87: coverage.py emits no arc for a conditional expression, so a region at 100%
statements and 100% branches can still hide an unexercised arm behind every
`x if y else z`. Sub-project 15 found six that way and one guarded a real `None`.

**Enumerate them by AST walk, not by grep** — harness fact 94, and the method
sub-project 16's reviewer used: `ast.parse` the module, walk for `ast.IfExp`
nodes inside the `FunctionDef` whose extent you derived from an unfiltered
`^def ` scan, and use `end_lineno` rather than "the next `def`". A textual
`' if .* else '` search misses ternaries in dict literals, f-strings,
comprehensions and argument defaults.

**The spec found three by eye and calls that a floor, not a count**: `:3410` and
`:3452` are both
`new_url = old_url if post.type == POST_TYPE_EVENT else None`, and `:3501` is
`old_domain = domain_from_url(old_url) if old_url else None`. **Report your own
count and any disagreement plainly.**

For each, test **both** arms or give a stated catalogued cause
(`tests/README.md` fact 75). The two `new_url` ternaries need a post whose
`type` is and is not `POST_TYPE_EVENT`; `:3501` needs `old_url` set and unset.
Assert on persisted state that differs between the arms.

**Mutation-prove each**: collapse the ternary to each arm in turn and confirm
exactly the new test dies. Record kill type and sole-or-multi. Note that a
collapse whose arms cannot differ observably is an **equivalent mutant** — say
so and name the cause rather than forcing a fixture.

- [ ] **Run and commit**

Run: `./run_tests.sh tests/test_ap_update_post_tails.py -q`
Expected: **6 tests added**, one per ternary arm pair, adjusted to your count.

```bash
git add tests/test_ap_update_post_tails.py
git commit -m "test: pin both arms of every conditional in the update tails"
```

---

### Task 9: Register the findings, add the harness facts, raise the floor

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `tests/test_ap_notify_post.py`
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: every earlier task's findings, via the SDD ledger.

**Write no tests and change no production code.** The one change to
`tests/test_ap_notify_post.py` is a single guard line, described below.

- [ ] **Step 1: Read the ledger in full**, then the per-task reports, then the
spec. **Register what was found, not what was predicted.**

- [ ] **Step 2: Write the entries, numbered from D284.** Read several existing
entries first and match the house style. Each states the defect, where it lives
**by content with a line citation correct at your commit**, how it was found,
whether fixed or registered, and if registered, why not.

At minimum, and the ledger will have more:

- **The poll block's three-loop guard asymmetry** and the fix, with its commit.
  Record the `sort_order` argument Task 4 made — the register is where a
  behaviour decision taken inside a "mechanical" fix has to be visible.
- **`poll.end_poll`'s raw peer string** assigned to a `DateTime` column
  (`:3338`), guarded only by a membership test, against the Event block's own
  `datetime.fromisoformat` spelling twenty-six lines below. **Establish whether
  the Event twin is itself a register entry** before calling it the correct
  spelling.
- **The Event block's thirteen consecutive unguarded subscripts**
  (`:3367-3379`, one per line). A peer document omitting any one raises
  `KeyError`. This is the unguarded-peer-input family's shape; **check the
  family index before writing a new entry** — it exists because this campaign
  has twice registered a defect whose sibling was already registered.
- **The duplicated `targets_data` literal** (`:3505-3510` and `:3523-3528`,
  byte-identical five-key dicts). D242/D262's duplication family at a fourth
  site. Check the family before writing.

**The file's correction convention** — D233, D234, D243, D253, D257 — appends a
**marked** correction rather than rewriting. But a cell that was *right when
written* and has since drifted **stands and is re-read before reuse**; only a
cell that was *wrong when written* is fixed in place. Sub-project 16 got this
distinction wrong in one direction and right in the other; read fact 95.

- [ ] **Step 3: Update the family index.** The unguarded-peer-input index is a
section in the register with seventeen members and thirty-one rejections,
partitioning D236 through D283 exactly. Any entry you add in that scope changes
the partition. **Re-derive the whole count rather than incrementing it**, and if
your entries fall inside the index's declared scope, extend both the membership
and the arithmetic. No existing entry is renumbered, moved or edited.

- [ ] **Step 4: Update every live "Next free number" note.** Sub-project 16
found **five** live notes where the prior count said four — the fifth was the
note that sub-project's own section had written rather than advanced. Re-run the
mechanical test; do not inherit the number. Say in your report how you told the
live ones from the frozen.

- [ ] **Step 5: Close sub-project 16's two parked residuals.**

- `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md:5613` states
  the nine `author` hits in `app/activitypub/util.py:2804-2936` include "five
  `author_id=` column keywords". There are **four** — `:2887` is
  `'author_id': post.user_id}`, a dict key in the `NOTIF_TOPIC` arm. The total
  of nine and D283's verdict are unaffected. **Verify before repairing**; the
  cell may have drifted, in which case find it by content.
- `tests/test_ap_notify_post.py:410`'s distinctness guard omits
  `post.instance_id`. Mutant P-M3 (`'post_id': post.instance_id`) dies today only
  because `post.instance_id == 1 == community.id`. Add `post.instance_id` to the
  guarded set and adjust the expected length. **Run
  `./run_tests.sh tests/test_ap_notify_post.py -q` afterwards** — this is the one
  test file this task touches, and it must stay green.

- [ ] **Step 6: Add the harness facts to `tests/README.md`**, which currently
runs to **95**. Add only what is not already there; **say which candidates you
dropped and why** — a redundant fact in a 95-fact file costs a reader more than a
missing one. Candidates from this slice:

- **`http_mock` asserts every registered route was called.** A test registering
  a route its path never reaches fails, which is a feature — but it means a test
  covering a partial fetch registers only the routes that fetch hits.
- **A retry with `time.sleep` costs real seconds per traversal** unless the sleep
  is patched, and the patch target depends on how the module imports `time`.
- **`Site.admins()` treats `User.id == 1` as an admin** without any role row,
  which silently makes the first-seeded user an admin in every fixture built on
  the `make_community` id-occupation pattern.
- **The two image-boundary techniques are not interchangeable** — one call site
  is gated on `cache_remote_images_locally` and the other is not.
- Anything else in the ledger's rulings that generalises.

- [ ] **Step 7: Raise the floor.** `coverage_floors.ini`,
`app/activitypub/util.py`: from **72** to the measured blended figure rounded
down. **The controller supplies that figure — do not run a coverage run.**

- [ ] **Step 8: Verify every citation.** Task 4 moved lines. Verify every
`app/*.py` citation against current source, and **verify function attributions
separately from line ranges**. Derive any function extent from an **unfiltered**
`^def ` scan. Read line numbers with `grep -n`, `awk` on `NR`, or `cat -n` —
never by counting out of a bare `sed -n 'A,Bp'`.

- [ ] **Step 9: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md tests/test_ap_notify_post.py coverage_floors.ini
git commit -m "docs: register sub-project 17's findings and raise the util.py floor"
```

---

## Self-review

**Spec coverage.** All five clusters have tasks: `Video` (Task 1), `Question`
(Tasks 2-3, fixed in 4), `Event` and the attachment dispatch (Task 5), the
url-change block (Task 6), the suspicious-domain tail (Task 7). The spec's
conditional-expression criterion is Task 8; its register work, family index,
floor and the two parked residuals are Task 9. Both spec asymmetries are
reached: the three-loop guard levels (Tasks 2-4) and `end_poll`'s raw assignment
(Task 3 observes, Task 9 registers). Every "Authorised for fixing" item is Task
4; every "Registered by default" item is named in Task 9 Step 2. The spec's ten
success criteria map to Tasks 1-7 (1, 2), Task 8 (3), Task 4 (4, 5), Task 9
(6, 7, 8, 9) and the controller's final run (10).

**Placeholder scan.** No "TBD", no "add appropriate error handling". **Seven
steps deliberately say "read X and decide/report"** — Task 1's third-copy
decision, Task 2's `mode` persistence question, Task 3's `endTime` behaviour,
Task 4's `sort_order` argument, Task 5's Event-factory absence, Task 6's
image-boundary choice, Task 7's `Site.admins()` arm and the `already_notified`
key space, and Task 8's ternary enumeration. Each names what to read, what to
produce, and what to report if the answer differs from this plan's.

**Type consistency.** `_seed_post(software='lemmy')` returns a `Post` and every
task uses it that way. `_update(**fields)` returns `{'object': fields}`;
`_poll_update(*choices, end_time=None, mode_key='oneOf')` and `_choice(name,
total=None)` are introduced in Task 2 and called with those signatures in Tasks
3 and 4. `_seed_event_post()` returns `(post, event)` and is named that way in
Tasks 5 and 6. `update_post_from_activity(post, request_json)` is called with
that arity throughout.

**Three things this plan does that its predecessors did not.**

1. **Task 4 states outright that its "mechanical" fix contains a behaviour
   choice.** The campaign's fix rule sorts on whether the correct spelling
   exists; the `sort_order` increment shows a case where it exists and the repair
   is still a decision. Making the implementer argue it, rather than discover it
   mid-edit, is the point.
2. **Task 6 owns the image boundary for the whole file**, and Task 5 is told to
   defer to it. Sub-project 16 learned that two tasks inventing separate
   techniques for one hazard is how a file ends up self-inconsistent.
3. **Task 2 forbids mutating the lines Task 4 will change.** Sub-project 15's
   Fix C unkilled a mutant that had been killed only through the defect the fix
   removed; recording kills against about-to-change code is how that starts.

# Sub-project 15: the create path's reply half — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover `create_post_reply` and `notify_about_post_reply` — 109
uncovered statements — fix the three defects sub-project 14 repaired in their
twin and left standing here, and fix D257 at both of its sites.

**Architecture:** One new test file, `tests/test_ap_create_reply.py`, driving
both functions by direct call. Tests first, across eight tasks; then one task
carrying the three in-scope fixes, one carrying D257 and a register
correction, and one recording the findings.

**Tech Stack:** pytest, `./run_tests.sh` (podman-compose, tmpfs Postgres),
`tests/factories.py`, the local redis lock double from
`tests/test_inbox_dispatch_votes.py`.

**Spec:** `docs/superpowers/specs/2026-09-04-coverage-create-reply-15-design.md`

## Global Constraints

- Defects found in these two functions are fixed test-first, each in its own
  commit, separate from every test-only commit, and each proved by a mutation
  that fails a named test. Anything outside them is registered, **except
  D257**, which is authorised by name.
- **A defect is fixed when the correct spelling already exists in the file and
  the change is mechanical; it is registered when the fix would require
  choosing new behaviour for a case the codebase has never handled.** Before
  ruling a defect register-only for want of a correct spelling, **grep the
  file for the twin**.
- Findings are numbered from **D260**, and **all** live "Next free number"
  notes are updated in the same change. The historical notes are frozen
  records — leave them alone.
- The coverage floor for `app/activitypub/util.py` rises to the measured
  blended figure rounded down. It currently reads **66**.
- Locate every code target by content, not by the line numbers in this plan.
- **Verify a citation's function attribution separately from its line range**,
  and derive a function's extent from an **unfiltered** scan of `^def `, never
  a filtered grep. Correcting a claim does not correct its copies — grep for
  the wrong claim, not only the right one.
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
stated running totals and corrected them five times; sub-project 14 used
deltas and corrected none. Each task says how many tests it adds, and the
implementer reports the actual file total it observed. **The delta is an
estimate, never a cap or a floor** — sub-projects 14's tasks needed more than
their briefs predicted six times out of nine, and were right every time.

## The harness fact that decides Task 1's design

`create_post_reply`'s five head guards all do the same two things: call
`log_incoming_ap(...)` and `return None`. Their return values are
indistinguishable, and so is their effect on the database — **unless the log
row is written**.

`log_incoming_ap` writes an `ActivityPubLog` row only when
`current_app.config['LOG_ACTIVITYPUB_TO_DB']` is true, and `config.py` defaults
it to `False`. **A test that sets it to `True` can assert which guard fired**,
because each guard passes a different message. A test that leaves it `False`
can only assert "no reply was created", which every one of the five guards
satisfies — five indistinguishable negatives, and no attributable kill for any
of them.

**Task 1 turns it on.** That is what makes the head guards mutation-testable
at all.

## File structure

| File | Responsibility |
|---|---|
| `tests/test_ap_create_reply.py` | **Create.** Every test in this slice except D257's. Helpers at the top, then `create_post_reply` in source order, then `notify_about_post_reply`. |
| `tests/test_ap_update_pair.py` | **Modify, Task 10 only.** D257's two pins, beside the guards they correct. |
| `app/activitypub/util.py` | **Modify, Tasks 9 and 10 only.** Four fixes, four commits. No other task touches it. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Tasks 10 and 11.** D250's correction, then entries from D260. |
| `tests/README.md` | **Modify, Task 11.** Harness facts; currently numbered to 80. |
| `coverage_floors.ini` | **Modify, Task 11.** `app/activitypub/util.py`, 66 → measured. |

---

### Task 1: The harness, and the five head guards

**Files:**
- Create: `tests/test_ap_create_reply.py`

**Interfaces:**
- Produces: `redis_lock_only_double` fixture; `PEER`, `_seed_scenario()`,
  `_reply_doc(**fields)`, `_create(**kwargs)` helpers, used by every later
  task.

**Read first, and report in one line each what they pin:** the seven existing
files that reference `create_post_reply` — `tests/test_visibility_ingest.py`,
`tests/test_backfill_reply_visibility.py`, `tests/test_utils_can_post.py`,
`tests/test_ap_resolve_from_search.py`, `tests/test_inbox_dispatch_new_content.py`,
`tests/test_ap_create_resolved_object.py`, `tests/test_ap_update_pair.py`.
Some reach it through the inbox rather than directly. **If any already pins a
head guard, say so and do not duplicate it.**

**Read the function's signature and its parent resolution before writing.**
`create_post_reply(store_ap_json, community, in_reply_to, request_json, user,
announce_id=None)`. `in_reply_to` is a URI resolved by `find_reply_parent`,
which branches on the **string content** of that URI — `'comment' in
in_reply_to` and `'post' in in_reply_to` are hints, with a fallback that tries
both lookups. Read it; the shape of your seeded `ap_id` values decides which
branch runs.

- [ ] **Step 1: Write the file's header, helpers and fixture**

```python
"""`create_post_reply` and `notify_about_post_reply` -- the create path's reply
half, and the mirror of the `update_post_reply_from_activity` that
sub-project 14 covered in full.

Entry is a direct call. `create_post_reply` takes an already-resolved
`community` and `user` plus the raw document, and resolves its parent from the
`in_reply_to` URI through `find_reply_parent`, which branches on whether the
string contains 'comment' or 'post'.

Both functions reach `redis_client.lock` -- `notify_about_post_reply` opens it
directly, `create_post_reply` through `PostReply.new`. The shared
`redis_double` fixture cannot serve a lock: fakeredis without lupa has no Lua
scripting and redis-py's `Lock.release()` issues an EVALSHA. Every test here
takes `redis_lock_only_double`, whose `.lock()` is a nullcontext.

`log_incoming_ap` writes an `ActivityPubLog` row only when
`LOG_ACTIVITYPUB_TO_DB` is true, and config.py defaults it to False. The head
guards are otherwise indistinguishable from each other -- each returns None
and creates nothing -- so the tests that pin them turn it on and assert the
message, which differs per guard.
"""
import contextlib

import pytest

from app import db
from app.activitypub.util import create_post_reply, notify_about_post_reply
from app.constants import NOTIF_MENTION, NOTIF_POST, NOTIF_REPLY
from app.models import ActivityPubLog, Notification, PostReply, User
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_post,
                             make_post_reply, make_site, make_user)

PEER = 'peer.example'


class _RedisLockOnlyDouble:
    """`app.redis_client` stand-in covering only `.lock(...)` as a context
    manager. Same shape as tests/test_inbox_dispatch_votes.py's, and for the
    same reason -- see this module's docstring.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


@pytest.fixture
def ap_log(app):
    """Turn on the ActivityPubLog write so a head guard is attributable.

    Without this, all five head guards return None and create nothing, which
    makes them indistinguishable from each other and from a guard that was
    deleted. With it, each writes its own message.
    """
    app.config['LOG_ACTIVITYPUB_TO_DB'] = True
    yield
    app.config['LOG_ACTIVITYPUB_TO_DB'] = False


def _seed_scenario(local_only=False):
    """A local community owned by user 1, a remote author, a remote replier,
    and one Post to reply to.

    `make_community` hardcodes `instance_id=1` and `user_id=1`, so an instance
    and a user are seeded first to occupy those ids -- the pattern
    tests/test_inbox_dispatch_votes.py documents.

    The post's `ap_id` deliberately contains 'post', because `find_reply_parent`
    branches on that substring being present in `in_reply_to`.
    """
    make_site()
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    community.local_only = local_only
    author = make_user(instance, 'author')
    replier = make_user(instance, 'replier')
    post = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, post, replier


def _reply_doc(**fields):
    """A Create activity's envelope plus its `object`.

    `create_post_reply` reads `request_json['id']` for the log and
    `request_json['object']` for everything else, so both levels matter here --
    unlike sub-project 14's helper, whose functions read only the object.
    """
    obj = {'id': f'https://{PEER}/comment/1', 'type': 'Note'}
    obj.update(fields)
    return {'id': f'https://{PEER}/activities/create/1',
            'type': 'Create',
            'object': obj}


def _create(community, post, replier, document=None, in_reply_to=None):
    """Call the function under test with the arguments its callers pass.

    `store_ap_json=True` so the log row carries the document, which is what
    makes a head-guard assertion able to name the document that provoked it.
    """
    return create_post_reply(
        store_ap_json=True,
        community=community,
        in_reply_to=in_reply_to if in_reply_to is not None else post.ap_id,
        request_json=document if document is not None else _reply_doc(content='hello'),
        user=replier,
    )
```

- [ ] **Step 2: Write the five head-guard tests**

```python
def test_a_local_only_community_discards_the_reply(app, db_session, redis_lock_only_double, ap_log):
    """The first guard. A local-only community takes no federated replies.

    Asserts the log row's message as well as the None return, because all five
    head guards return None and create nothing -- the message is the only thing
    that says WHICH guard fired.
    """
    community, post, replier = _seed_scenario(local_only=True)

    result = _create(community, post, replier)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert 'local only' in log.exception_message


@pytest.mark.parametrize('visibility_field,expected', [
    ({'to': [], 'cc': []}, 'followers'),
])
def test_a_non_public_reply_is_refused(app, db_session, redis_lock_only_double, ap_log,
                                       visibility_field, expected):
    """The visibility guard, which reads `activitypub_visibility(request_json
    .get('object'))` and refuses 'followers' and 'direct'.

    Read `activitypub_visibility` before choosing the document -- the
    parametrised case above is a starting point and the addressing keys that
    produce each verdict must come from that function, not from this plan.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello', **visibility_field)

    result = _create(community, post, replier, document=document)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert expected in log.exception_message


def test_an_unresolvable_parent_is_refused(app, db_session, redis_lock_only_double, ap_log):
    """`find_reply_parent` returns three Nones for a URI matching nothing, so
    `post_id is None` and the function gives up.

    The URI deliberately contains neither 'comment' nor 'post', so every branch
    of `find_reply_parent` misses.
    """
    community, post, replier = _seed_scenario()

    result = _create(community, post, replier, in_reply_to=f'https://{PEER}/nothing/1')

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert 'parent post' in log.exception_message


def test_a_reply_to_an_archived_post_is_refused(app, db_session, redis_lock_only_double, ap_log):
    """`post.archived` is seeded True; the column's default is False, so the
    seed is what makes this guard reachable rather than incidental.
    """
    community, post, replier = _seed_scenario()
    post.archived = True
    db.session.commit()

    result = _create(community, post, replier)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert 'archived' in log.exception_message


def test_a_reply_from_a_blocked_user_is_refused(app, db_session, redis_lock_only_double, ap_log):
    """`post.author.has_blocked_user(user.id)`. Read `make_user_block`'s
    signature and `has_blocked_user`'s query before writing the seed -- which
    way round the block is stored decides whether this test provokes the guard
    or passes for the wrong reason.
    """
    community, post, replier = _seed_scenario()
    # seed the block here; see Step 3

    result = _create(community, post, replier)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert 'blocked' in log.exception_message
```

- [ ] **Step 3: Finish the block test's seeding, and the visibility parametrisation**

Two tests above are deliberately unfinished, because the plan will not guess.
Read `make_user_block` in `tests/factories.py` and `has_blocked_user` in
`app/models.py`, then write the two or three lines that make the post's author
block the replier. Read `activitypub_visibility` and replace the parametrised
case with the addressing that actually yields `'followers'`, and add a
`'direct'` case if that verdict is separately reachable.

**Nothing with a placeholder may be committed.** If `has_blocked_user` keys
the opposite way from what the test name implies, say so in your report — that
is itself a finding.

- [ ] **Step 4: Mutation-test the five guards**

Delete each guard's condition alone, run, confirm exactly one named test
fails, restore, confirm `git diff -- app/` is empty. **The `or` in the block
guard has two operands** — `has_blocked_user` and `has_blocked_instance` — so
three kills are needed at that site, not one. If the instance half has no test,
add one rather than accepting the gap.

- [ ] **Step 5: Run and commit**

Run: `./run_tests.sh tests/test_ap_create_reply.py -q`
Expected: **6 tests added**, plus any Step 4 showed were needed.

```bash
git add tests/test_ap_create_reply.py
git commit -m "test: cover create_post_reply's head guards"
```

---

### Task 2: The body arm and the language resolution

**Files:**
- Modify: `tests/test_ap_create_reply.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixtures.

Two blocks. The body arm wraps bare content in `<p>`, allowlists it, then
prefers a markdown `source` if one is present. The language block is an
if/elif/else: a `language` dict, then a `contentMap` fallback, then
`site_language_id()`.

**Do NOT write a test for `"content": null`.** That is the crash Task 9 fixes,
and its pin belongs with the fix.

**The language block reads `find_language_or_create(...).id`, which is the
second defect Task 9 fixes** — the row is added without a flush and the app
factory sets `autoflush=False`, so `.id` is `None`. Work around it here by
pre-seeding and committing the `Language` row so the "already exists" branch
runs, and **say so in the docstring**: a workaround that reads as ordinary
setup is the failure this campaign keeps finding.

- [ ] **Step 1: Write the body-arm tests**

```python
def test_bare_content_is_wrapped_and_allowlisted(app, db_session, redis_lock_only_double):
    """Content that starts with neither `<p>` nor `<blockquote>` is wrapped
    before allowlisting, and `body` is derived from the html because no
    `source` was supplied.
    """
    community, post, replier = _seed_scenario()

    reply = _create(community, post, replier, document=_reply_doc(content='hello there'))

    assert reply.body_html == '<p>hello there</p>'
    assert reply.body == 'hello there'


def test_already_wrapped_content_is_not_double_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<p>')` disjunct of the wrap guard."""
    community, post, replier = _seed_scenario()

    reply = _create(community, post, replier, document=_reply_doc(content='<p>hello</p>'))

    assert reply.body_html == '<p>hello</p>'


def test_blockquote_content_is_not_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<blockquote>')` disjunct, which needs its own test or
    it can be deleted with the `<p>` case still passing.
    """
    community, post, replier = _seed_scenario()

    reply = _create(community, post, replier,
                    document=_reply_doc(content='<blockquote>q</blockquote>'))

    assert reply.body_html.startswith('<blockquote>')


def test_a_markdown_source_overwrites_the_html_derived_body(app, db_session, redis_lock_only_double):
    """`source` with `mediaType: text/markdown` wins, overwriting the body the
    html arm computed. The two strings differ deliberately, or the assertion
    could not tell which arm won.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='<p>from html</p>',
                          source={'mediaType': 'text/markdown', 'content': 'from markdown'})

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'from markdown'
    assert 'from markdown' in reply.body_html
    assert 'from html' not in reply.body_html


def test_a_source_with_no_media_type_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `'mediaType' in ...` conjunct, which THIS function has and its
    update-path twin lacked until sub-project 14 added it.

    A dict `source` carrying only `content` must fall to the `else`.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='<p>from html</p>', source={'content': 'from markdown'})

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'from html'


def test_a_source_that_is_not_a_dict_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` conjunct. Choose the fixture value with
    care: `in` against a string is a SUBSTRING test, so a string `source` lets
    the next conjunct return False rather than raise, and the guard
    short-circuits identically with or without `isinstance`. Use a value on
    which `'mediaType' in ...` raises or succeeds-then-fails.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='<p>from html</p>', source=None)

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'from html'
```

- [ ] **Step 2: Write the language tests**

Three arms and two `isinstance` conjuncts. Read `find_language`,
`find_language_or_create` and `site_language_id` first, and **report which
`Language` rows exist in the test database** — `find_language` looks up rather
than creating, so the `contentMap` code must already be there.

Cover: a `language` dict applied; a non-dict `language` ignored; `contentMap`
supplying the language when `language` is absent; `language` winning over
`contentMap` when both are present, with **different** languages named so the
winner is observable; and the `else` giving `site_language_id()` when neither
key is present.

Each test asserts `reply.language_id` against a **specific expected id**, not
merely "not None" — and the "ignored" cases seed a contrary baseline, since
`PostReply.language_id` has no column default.

- [ ] **Step 3: Mutation-test both blocks**

Each wrap disjunct, each `source` conjunct, each language arm and each
`isinstance`. One at a time; record kill type and sole-or-multi.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_create_reply.py -q`
Expected: **11 tests added.**

```bash
git add tests/test_ap_create_reply.py
git commit -m "test: cover the create reply's body and language arms"
```

---

### Task 3: The attachment loop

**Files:**
- Modify: `tests/test_ap_create_reply.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixtures.

Seventeen uncovered statements, and **structurally identical to the loop
sub-project 14 covered in its twin** — dict-or-list normalisation, `href` then
`url` with the second overwriting the first, `name` as alt text, an `if url:`
gate, and a `body_html` regeneration gated on the list being non-empty.

**Read it from source anyway.** The twin's loop is the same shape, but this
plan has been wrong about a fixture before, and one difference matters: here
`body` is a local that has just been built by the body arm, not a column read
back from the row.

**Sub-project 14 found the regeneration gate unkillable by its brief's six
tests**, because none of them asserted `body_html` after a *non-empty*
attachment list — only `body`, which `html_to_text` renders identically either
way. Write that test.

- [ ] **Step 1: Write the tests**

Cover: a single attachment dict appended; a list of two appended in order;
`url` winning over `href` when both are present, with different values; `href`
used alone when there is no `url`; an attachment with neither contributing
nothing, asserted by **exact equality** on `body` so an empty `![alt]()` fails
rather than passing unnoticed; `name` supplying alt text; an empty list leaving
`body_html` as the body arm computed it; and a non-empty list **regenerating**
`body_html`, asserted on `body_html` rather than `body`.

Every test asserts on the persisted `PostReply`, not on a local.

- [ ] **Step 2: Mutation-test the loop's guards**

The `isinstance(dict)` / `isinstance(list)` pair, the `'href' in`, `'url' in`
and `'name' in` gates, the `if url:` gate and the `if attachment_list:` gate.
One at a time, each killed by a distinct named test.

**Consider the neither-dict-nor-list case** — a scalar `attachment` — and say
whether it is a benign skip or a crash here. Sub-project 14 registered the
same case in its twin as D251; if this copy behaves differently, that is a
finding.

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_create_reply.py -q`
Expected: **8 tests added.**

```bash
git add tests/test_ap_create_reply.py
git commit -m "test: cover the create reply's attachment loop"
```

---

### Task 4: The Mention collection and the user flair

**Files:**
- Modify: `tests/test_ap_create_reply.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixtures.
- Produces: `_seed_local_recipient(name)` and `_mention(name)`, used by
  Tasks 5 and 6.

Two independent blocks. The Mention block is **two-phase here** — it collects
matching profile ids into `local_users_to_notify` and notifies later, where the
twin notifies inline. This task covers the collection phase; Tasks 5 and 6
cover what happens to the collected list.

The flair block reads `request_json['object']['flair']`, updates an existing
`UserFlair` row or creates one, and commits.

- [ ] **Step 1: Write the helpers and the Mention-collection tests**

```python
def _seed_local_recipient(name='localuser'):
    """A local user the Mention block can resolve.

    The lookup is `filter_by(ap_profile_id=..., ap_id=None)`, so both columns
    matter. `make_user(None, name, local=True)` leaves `ap_id` None and
    `ap_profile_id` None; this helper sets the profile id to the lowercased
    form the document will send.
    """
    recipient = make_user(None, name, local=True)
    recipient.ap_profile_id = f'https://test.piefed.local/u/{name}'
    db.session.commit()
    return recipient


def _mention(name='localuser'):
    return {'type': 'Mention', 'href': f'https://test.piefed.local/u/{name}'}
```

Cover, each asserting on the resulting `Notification` rows since
`local_users_to_notify` is a local: a Mention of a local user producing a
notification; the `len(tag) > 1` gate skipping a **lone** Mention entirely
(this pins current behaviour and the docstring must say so, not endorse it);
a Mention of a remote user producing none; a Mention with no `href` producing
none; a non-string `href` producing none; and the self-mention exclusion —
`profile_id != reply_parent.author.ap_profile_id` — producing none when the
mentioned user authored the parent.

**One trap from sub-project 14, which cost it a test.** A remote-host Mention
does not kill the `startswith` mutant, because a foreign host finds no local
recipient either way. Use a **same-host, case-mismatched** href to kill that
guard, and keep the remote-host test for what it does prove.

- [ ] **Step 2: Write the flair tests**

Cover: a flair on a user with none, creating a `UserFlair` row; a flair on a
user who already has one in that community, **updating** rather than creating,
asserted by a row count of exactly one plus the new value; and an absent or
falsy `flair` key leaving the count at a seeded non-zero baseline.

Read `UserFlair`'s model and `tests/factories.py` for a factory before seeding
one by hand.

- [ ] **Step 3: Mutation-test both blocks**

The tag gate's three conjuncts, the Mention guards, the self-mention
comparison, the flair block's `and request_json['object']['flair']` truthiness
half, and the `if existing_flair:` branch. One at a time.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_create_reply.py -q`
Expected: **9 tests added.**

```bash
git add tests/test_ap_create_reply.py
git commit -m "test: cover the create reply's mention collection and flair"
```

---

### Task 5: The notification block's resolution and delivery

**Files:**
- Modify: `tests/test_ap_create_reply.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 4's `_seed_local_recipient` and `_mention`.

The block that consumes `local_users_to_notify`, inside the `try` that wraps
`PostReply.new`. This task covers reaching the notification and the guards
around it; Task 6 covers the four suppression rules inside.

Cover: the notification created, asserting the row's recipient, `notif_type`,
`subtype` and `url`; the unread counter incremented, seeded to a non-default
**3** and asserted **4**, so "incremented" is distinguishable from "set to 1";
a recipient that does not resolve producing nothing; a blocked sender
suppressed, with the block direction read from `blocked_users` rather than
assumed; an existing notification for the same comment not duplicated; and the
`force_locale(get_recipient_language(...))` wrapper actually reached.

**The de-duplication block is gated on the sending instance.** Read the
condition and say in your report which software values reach it and which do
not, then keep this task on the ungated path and leave the rules to Task 6.

**The tail `except Exception as ex`** is this function's and not its twin's.
Do not write a test that provokes it here — first read what it does with the
exception and report it, because whether it swallows a real failure is a
finding, and pinning it before that question is answered would freeze
behaviour nobody has decided.

- [ ] **Step 1: Write the tests, Step 2: mutation-test, Step 3: run and commit**

Run: `./run_tests.sh tests/test_ap_create_reply.py -q`
Expected: **6 tests added.**

```bash
git add tests/test_ap_create_reply.py
git commit -m "test: cover the create reply's mention notification"
```

---

### Task 6: The four suppression rules

**Files:**
- Modify: `tests/test_ap_create_reply.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 4's `_seed_local_recipient` and `_mention`.

The de-duplication rules, gated on the instance's software. **This copy's rule
3 is the one that works** — its query and `continue` sit outside the
`for element in ... .path` loop, which is the spelling sub-project 14 copied
into the update path as its Fix F. Read both and confirm before writing.

**Write the opt-in control test first** and watch it create a notification, or
all four suppression tests are negative against nothing.

Then one test per rule, each seeding exactly the state its rule keys on and
asserting **zero** notifications for the recipient. Derive each from source:
the recipient is the post's author; a `post_mention` already exists for this
post; a `comment_mention` already exists anywhere in the comment chain; and the
recipient authored a comment in that chain.

**Two traps this campaign has paid for, both of which bite here.** A negative
test can fail to kill its guard because a later rule independently produces the
same "no notification" — check each fixture against *every* downstream route,
not just the one its rule names. And rules 3 and 4 both depend on the shape of
`reply.path`; **report what you found it to be** and how you seeded it, since
`make_post_reply` sets neither `path` nor `parent_id`.

**Rule 4 is where D243 lives** — the empty-tuple `IN ()`. A top-level reply
gives an empty `ids`, and Postgres rejects `IN ()`. **Pin the crash here**;
Task 9 fixes it and inverts this pin.

- [ ] **Step 1: Write the control and the four rules, Step 2: pin D243's crash,
  Step 3: mutation-test each rule separately, Step 4: run and commit**

Run: `./run_tests.sh tests/test_ap_create_reply.py -q`
Expected: **6 tests added** — the control, four rules, and D243's crash pin.

```bash
git add tests/test_ap_create_reply.py
git commit -m "test: cover the create reply's mention de-duplication"
```

---

### Task 7: `notify_about_post_reply`, the top-level branch

**Files:**
- Modify: `tests/test_ap_create_reply.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixtures.

`notify_about_post_reply(parent_reply, new_reply)` branches on
`parent_reply is None`. This task covers that branch — a top-level comment on
a post — which notifies everyone subscribed to the post.

Call it **directly**, not through `create_post_reply`: the two arguments are
rows, and driving it end-to-end would make the assertions depend on everything
upstream.

Cover: a subscriber notified, asserting the row's `notif_type`, `subtype`,
`url` and `targets`; the author of the reply **not** notified even when
subscribed, which is the `new_reply.user_id != notify_id` guard; the unread
counter incremented from a seeded non-default baseline; and no subscribers
producing no rows, with a seeded non-zero `Notification` count elsewhere so
"none created" is distinguishable from "none exist".

Read `notification_subscribers` and seed subscriptions through the factory
rather than by hand if one exists.

- [ ] **Step 1: Write the tests, Step 2: mutation-test, Step 3: run and commit**

Run: `./run_tests.sh tests/test_ap_create_reply.py -q`
Expected: **4 tests added.**

```bash
git add tests/test_ap_create_reply.py
git commit -m "test: cover the top-level branch of notify_about_post_reply"
```

---

### Task 8: `notify_about_post_reply`, the reply branch

**Files:**
- Modify: `tests/test_ap_create_reply.py`

**Interfaces:**
- Consumes: Task 1's helpers and fixtures.

The `else` — a reply to a comment. It does three things the top-level branch
does not: marks the parent's notifications read for the new reply's author,
**recounts** that author's unread total from the database rather than
incrementing it, and notifies the parent comment's subscribers.

Cover each separately. The mark-read test seeds an unread `Notification` whose
`targets['comment_id']` matches the parent and asserts `read` became True. The
recount test is the interesting one: seed `unread_notifications` to a value
that **disagrees** with the real unread count, so a recount and an increment
produce different answers — that is the only way to prove which happened.
Then the subscriber notification, its `notif_type` and `subtype`, and the
author-excluded guard.

The recount is the one shape worth spelling out, because "recounted" and
"incremented" are indistinguishable unless the seed disagrees with the truth:

```python
def test_the_authors_unread_total_is_recounted_not_incremented(app, db_session, redis_lock_only_double):
    """The reply branch RECOUNTS `unread_notifications` from the database
    where the top-level branch increments it.

    The seeded counter is deliberately wrong -- 9 against a real unread count
    of 1 -- because an increment would give 10 and a recount gives the true
    figure. A seed that agreed with the truth could not tell them apart.
    """
    ...  # seed a parent reply, a new reply by `author`, and exactly one
         # unread Notification for `author`; see Step 1
    author.unread_notifications = 9
    db.session.commit()

    notify_about_post_reply(parent_reply, new_reply)

    assert author.unread_notifications == Notification.query.filter_by(
        user_id=author.id, read=False).count()
    assert author.unread_notifications != 10
```

- [ ] **Step 1: Finish that test's seeding and write the other four,
  Step 2: mutation-test, Step 3: run and commit**

The `...` above is the seeding this plan will not guess — read
`notification_subscribers` and the `Notification.targets['comment_id']`
comparison in the mark-read statement first. **Nothing with a placeholder may
be committed.**

Run: `./run_tests.sh tests/test_ap_create_reply.py -q`
Expected: **5 tests added.**

```bash
git add tests/test_ap_create_reply.py
git commit -m "test: cover the reply branch of notify_about_post_reply"
```

---

### Task 9: The three mirrored fixes

**Files:**
- Modify: `app/activitypub/util.py`
- Modify: `tests/test_ap_create_reply.py`

**Interfaces:**
- Consumes: Task 1's helpers, Task 6's D243 crash pin.

**The first task that changes production code. THREE FIXES, THREE SEPARATE
COMMITS.** Each is a defect sub-project 14 repaired in this function's twin
and left standing here, so each has a correct spelling in the same file.

**Fix A — `"content": null`.** `create_post_reply` calls `.startswith` on the
value; the twin guards `is not None`. Add the same conjunct.

**Fix B — the unflushed `Language.id`.** `find_language_or_create` adds without
flushing and the factory sets `autoflush=False`, so `.id` is `None` and the
reply is created with no language. Sub-project 14 fixed the same read by
assigning through the relationship — **but here the id is passed to
`PostReply.new(language_id=...)`, so that shape may not be available.** Read
`PostReply.new`'s signature first. If a mechanical repair exists, make it; if
it would mean choosing new behaviour, **register it instead and say why** —
inventing a shape the file does not already use is the thing the fix rule
forbids.

**Fix C — D243, the empty-tuple `IN ()`.** Add the `if ids:` guard, the same
spelling sub-project 14 used in the twin. **Task 6's crash pin must be
inverted in this same commit**, or it fails the moment the guard lands.

Per fix, four steps: write or invert the pin asserting persisted row state;
run it and **quote the failure verbatim**; apply the fix; mutation-test by
reverting the fix alone and confirming the pin fails and nothing else does,
then restore and confirm `git diff -- app/activitypub/util.py` shows only the
intended change.

```bash
git commit -m "fix: skip a create reply whose content is null"
git commit -m "fix: give a create reply the language its document names"
git commit -m "fix: skip the ancestor lookup when a create reply has no ancestors"
```

- [ ] **Step 13: Check for a vacated branch.** Each fix may remove the only
  path some branch was reached by, and **adding a conjunct can unkill an
  existing test** — sub-project 14 hit both. Re-run each changed guard's
  *existing* mutations, not only the new one.

- [ ] **Step 14: Audit the docstrings the fixes falsified.** Every claim in
  either test file that this function crashes, or that it differs from its
  twin in one of these three ways, is now suspect. Grep both
  `tests/test_ap_create_reply.py` and `tests/test_ap_update_pair.py` for
  `crash`, `raise`, `unguarded`, `unflushed`, `twin`, `sub-project 14`, and
  read every hit. **Correcting a claim does not correct its copies** — grep
  for the wrong claim, not only the right one.

---

### Task 10: D257, and the D250 correction

**Files:**
- Modify: `app/activitypub/util.py`
- Modify: `tests/test_ap_update_pair.py`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**Interfaces:**
- Consumes: nothing from this slice's earlier tasks — D257 lives in the update
  pair, not the create path.

**ONE FIX, then one register correction, in separate commits.**

**D257 — `datetime.fromisoformat` is wrapped in `except ValueError`, but a
non-string `updated` raises `TypeError`, which escapes.** Two sites, one in
each update function. The repair is `except (ValueError, TypeError)` — one
token each — and it is the only one of sub-project 14's five late crashes whose
intended behaviour the file already states, in the `except` clause below it.

Write **two** pins, one per site, in `tests/test_ap_update_pair.py` beside the
tests that already cover those ternaries. Each serves a non-string `updated` —
an integer is enough — and asserts the row's `ap_updated` fell back to now,
which is what the existing `except ValueError` arm's test asserts for its own
case. Watch both fail with `TypeError`, quote both verbatim, fix, then
mutation-test **each site separately**: two call sites, two kills.

```bash
git commit -m "fix: fall back when a peer sends a non-string updated timestamp"
```

**Then correct D250.** Sub-project 14 registered the `contentMap` fallback as a
pair asymmetry — post-only against the update-reply. It is three-way:
`create_post_reply` has it too. **Append a marked correction** following the
file's convention, which D233, D234 and D243 use — do not rewrite the original
prose.

```bash
git commit -m "docs: correct D250 to the three-way contentMap asymmetry"
```

---

### Task 11: Register the findings, record the harness facts, raise the floor

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: every earlier task's findings, via the SDD ledger.

**Write no tests and change no production code.**

- [ ] **Step 1: Read the ledger in full**, then the per-task reports, then the
spec. **Register what was found, not what was predicted.**

- [ ] **Step 2: Write the entries, numbered from D260.** Read several existing
entries first and match the house style. Each states the defect, where it
lives **by content with a line citation correct at your commit**, how it was
found, whether it was fixed or registered, and if registered, why not.

At minimum, and the ledger will have more: the three mirrored fixes with their
commits; D257 with its; the `site_language_id()` fallback asymmetry; the
two-phase Mention collection; the `distinguished` default difference; the
absent `repliesEnabled`; and the bare `except Exception as ex` tail, with
whatever Task 5 found it swallows.

- [ ] **Step 3: Update every live "Next free number" note.** Sub-project 14
left three live ones and eight frozen. Say in your report how you told them
apart.

- [ ] **Step 4: Add the harness facts to `tests/README.md`**, which currently
runs to **80**. The strongest candidate from this slice:
**`log_incoming_ap` writes nothing unless `LOG_ACTIVITYPUB_TO_DB` is on**, so
a guard that only logs and returns is indistinguishable from a deleted guard
until a test turns it on. Add whatever else the ledger's rulings generalise to,
and **say which candidates you dropped and why** — a redundant fact in an
80-fact file costs a reader more than a missing one.

- [ ] **Step 5: Raise the floor.** `coverage_floors.ini`,
`app/activitypub/util.py`: from **66** to the measured blended figure rounded
down. **The controller supplies that figure — do not run a coverage run.**

- [ ] **Step 6: Verify every citation.** Tasks 9 and 10 moved lines. Verify
every `app/*.py` citation against current source, and **verify function
attributions separately from line ranges** — this campaign has shipped false
attributions whose line numbers were correct three times, one of them written
as a correction. Derive any function extent from an **unfiltered** `^def `
scan.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 15's findings and raise the util.py floor"
```

---

## Self-review

**Spec coverage.** Both functions have tasks: `create_post_reply`'s head
guards (1), body and language (2), attachment loop (3), Mention collection and
flair (4), notification delivery (5), suppression rules (6);
`notify_about_post_reply`'s two branches (7, 8). The spec's three authorised
fixes are Task 9; D257 and the D250 correction are Task 10; register, facts
and floor are Task 11. Every row of the spec's asymmetry table is reached:
null `content` (2, 9), unflushed `Language.id` (2, 9), empty `IN ()` (6, 9),
rule 3's placement (6), `contentMap` (2, 10), `site_language_id()` (2, 11),
two-phase Mentions (4, 11), `distinguished` (2, 11), `repliesEnabled` (11),
`source['mediaType']` (2), the `except Exception` tail (5, 11). The spec's nine
criteria map to Tasks 1-8 (1-3), Task 9 (4), Task 10 (5, and criterion 6's
D250 half), Task 11 (6-8) and the controller's final run (9).

**Placeholder scan.** No "TBD", no "add appropriate error handling". **Six
steps deliberately say "read X and finish this"** — Task 1's block seeding and
visibility parametrisation, Task 2's language rows, Task 4's flair factory,
Task 5's gate condition and `except` investigation, Task 6's `reply.path`
shape, and Task 9's Fix B shape. Each names what to read, what to produce, and
what to report if the answer differs from the plan's guess. Task 9's is the
sharpest: it explicitly authorises **registering instead of fixing** if the
mechanical repair is not available, because inventing a shape the file does
not use is what the fix rule forbids.

**Tasks 3 and 5 through 8 specify their tests in prose rather than in verbatim
code, and that is a deliberate departure from the plan format, taken on
evidence.** Sub-project 14's plan supplied verbatim test code, and that code
was **wrong three times** in ways the implementers caught by running it: a
fixture chosen to land under a length threshold that actually produced a
22-character fallback, a column name that did not exist on the path under test,
and a flair-tag key the resolver never reads. The tasks specified in prose
produced *more* tests than estimated and no wrong fixtures, because the
implementer read the source instead of transcribing a guess.

So the rule this plan follows: **supply verbatim code where the shape is
non-obvious and the plan has read the source that fixes it** — Task 1's
helpers and head guards, Task 2's body arm, Task 8's recount — and specify in
prose where the shape is a known idiom and the risk is a stale fixture. Every
prose task still states what to cover, what to assert, what to seed, and what
to report.

**Type consistency.** `_seed_scenario(local_only=False)` returns
`(community, post, replier)` and every task unpacks it that way.
`_reply_doc(**fields)` and `_create(community, post, replier, document=None,
in_reply_to=None)` are called with those signatures throughout.
`_seed_local_recipient(name)` and `_mention(name)` are produced by Task 4 and
consumed by Tasks 5 and 6 with matching defaults. `create_post_reply` takes
`(store_ap_json, community, in_reply_to, request_json, user, announce_id)` and
`notify_about_post_reply` takes `(parent_reply, new_reply)` — both used with
that arity.

**One thing this plan does that sub-project 14's did not.** The harness
decision in Task 1 — turning on `LOG_ACTIVITYPUB_TO_DB` — is stated as its own
section before the tasks, because without it five separate guards collapse into
one indistinguishable observable and no head-guard mutation is attributable.
That is the kind of fact sub-project 14 discovered mid-task three times; here
it is settled up front.

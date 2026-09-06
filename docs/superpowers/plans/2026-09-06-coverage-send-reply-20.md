# Sub-project 20 — `send_reply` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take `send_reply` (`app/shared/tasks/notes.py:80-229`) to zero uncovered statements and branch arms except any proved unreachable, and land at most one production guard, decided by measurement.

**Architecture:** One new test file, `tests/test_shared_tasks_send_reply.py`, calls `send_reply` directly with an explicit `session`. The harness transfers almost whole from sub-project 19's `tests/test_shared_tasks_send_post.py` — the same capture-on-serialized-bytes helpers, the same autouse DNS stub, the same seeding-order rules. `send_reply` is `send_post`'s twin, so the divergences between them are the findings.

**Tech Stack:** pytest, respx (`http_mock`), SQLAlchemy, Flask, Celery (eager), `tests/factories.py`, `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-09-06-coverage-send-reply-20-design.md`

---

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- **Delete nothing the task did not create. `claude_test` in the repository root is not the campaign's.**
- Only the controller runs the full suite, one pytest session at a time, and **in the foreground**. Sub-project 19's Ruling 19: five background full-suite runs were killed by the harness's background-task memory guard while the system had 18Gi free and zero swap in use; every foreground run succeeded. The 407s suite fits inside the 600s tool timeout.
- **A wedged podman stack reports test failures that are not regressions.** Sub-project 19 saw an 8-file probe report "6 failed, 65 passed, 1 error in 618.53s" where the same files on a reset stack give 629 passed in 29.07s — the wedged run had collected 72 tests where the files hold 629. Before believing any failure, run `./run_tests.sh --down` and retry.
- Any run at or over ~600s is erroneous — that is `session_timeout` in `pytest.ini:28`, and pytest **still exits 0** on a session timeout, so a truncated run reads as green. Check the test count and the coverage report's mtime, never the exit code.
- Coverage command: `./run_tests.sh --cov=app --cov-branch --cov-report=json:scratch_full_cov.json -q`.
- **Mutation instrument.** Apply each mutation with a targeted single-line edit (`sed -i 'NNNs/old/new/'` or an Edit matching one unique line). **Never rewrite the whole file.** `app/shared/tasks/notes.py` is **310 lines**; assert that after every apply and every restore, together with an empty `git diff -- app/`. One at a time, never batched. A sub-project-18 implementer truncated a 1174-line production module at this exact step.
- **Commit the fix before running the mutations** (sub-project 19's Ruling 7): the clean-tree assertion is impossible while the fix is uncommitted, because `git checkout -- app/` would discard it.
- Mutation record: the mutation, the single test that killed it, **assertion-kill or crash-kill**, **sole or multi**.
- **Use `ast.parse` and `FunctionDef.end_lineno` for a function's extent, never a convention.** Sub-project 19 committed three different extents for one function across its own documents because three different rules were applied, and every document was internally consistent.
- Every line number copied from anywhere must be re-derived against the current tree before it is written down (fact 99). Citation sweeps run in two passes, `file:line` then bare paths against `git ls-files` (fact 100), and **grep for the value being corrected, not for the text you are editing**.
- Enumerate conditional expressions by AST walk, not by grep (fact 94) — coverage.py emits no arc for one (fact 87).
- Commit messages containing backticks are committed with `git commit -F <file>`, never `-m`.

---

## The uncovered map

Measured by the controller. **Nothing in the repository reaches `send_reply` today** — the whole function is uncovered. 134 total, 86 statements and 48 branch arms.

| Region | Lines | stmt | br | Task |
|---|---|---|---|---|
| Entry, parent dispatch, mention extraction | `:81-116` | 31 | 16 | 1 |
| Mention notification | `:118-141` | 21 | 16 | 3 |
| The three early returns | `:143-151` | (in the row above) | | 4 |
| The Note and Create builders | `:153-199` | 18 | 4 | 5 |
| Announce construction and delivery | `:201-229` | 16 | 12 | 6 |

The `:118-151` band's 21/16 covers both the notification block and the three returns; Tasks 3 and 4 split it by concern.

---

## Harness facts this plan depends on

Each was re-derived against the current tree while this plan was written.

1. `app/shared/tasks/notes.py:80` is `def send_reply(reply_id, parent_id, edit=False, session=None):`, and `ast` gives it `end_lineno` **229**. The file is **310 lines**.
2. **`session` has no usable default** — `:81` is `session.query(PostReply).filter_by(id=reply_id).one()`.
3. **`parent_id` is a branch, not a convenience.** `:83-86`: truthy loads a `PostReply` as the parent; falsy uses `reply.post`. Both arms must be exercised, and the parent's type changes what `:175` `parent.public_url()` produces.
4. **`recipients` starts seeded**, unlike `send_post`'s empty list: `:90` is `recipients = [parent.author]`. Then `:120` is `if recipient.is_local() and recipient.id != parent.author.id:` — so the parent's author is always a recipient for delivery but is deliberately excluded from the mention notification. That interaction has no analogue in `send_post`.
5. **Three early returns**, not four: `:143-144` (`community.local_only or not community.instance.online()`), `:147-148` (a `CommunityBan` row), `:149-151` (a remote community whose instance is blocked or banned). `send_post` has a fourth because it also tests `community.private`; this function does not — that is the spec's §2.2 finding.
6. `Instance.online()` (`app/models.py:118-119`) is `not (self.dormant or self.gone_forever)`, and both columns default `False` (`:98`, `:100`), so a factory instance is online without help.
7. **`:216`'s `instance.online()` is redundant.** `:216` calls `community.following_instances()` with the default `include_dormant=False`, so `app/models.py:849` filters `dormant == False` and `:850` filters `id != 1` and `gone_forever == False`. The conjunct cannot be False. Do not write a test for it; register it.
8. **`:203`/`:225` are the `@context` `del`/re-add pair.** Sub-project 19 proved the matching pair in `send_post` yields an **equivalent mutant**: `post_request` (`app/activitypub/signature.py:100-101`) re-adds `@context` when a body lacks it, same value, same trailing position, before serialization. Assert **key order**, and say in the docstring what cannot be pinned. Both arms of `:225` are reachable — `:195` puts `@context` in the literal and `:203` deletes it only when `community.is_local()`.
9. **No amendment block.** `send_reply` is already a Note, so there is no analogue of `send_post`'s `:309-330`, and sub-project 19's aliasing hazard cannot arise. `:203`'s `del` still mutates `create`, so keep asserting on serialized bytes.
10. `tests/factories.py:453` is `make_post_reply(post, user, body='a reply')`, which sets `user_id`, `post_id`, `community_id`, `instance_id`, `body`, `posted_at`, `deleted`. It does **not** set `parent_id` or `depth`.
11. `Community.is_local()` (`app/models.py:795-796`) is a **disjunction**: `ap_id is None` **or** `profile_id().startswith(SERVER_URL)`. Making a community remote means setting `ap_profile_id` too, not `ap_id` alone.
12. `tests/conftest.py:143` truncates with `RESTART IDENTITY`, so ids restart at 1 (fact 89). Seed the local instance **before** any peer, because `make_community` hardcodes `instance_id=1`.
13. `http_mock` is `assert_all_called=True` (`tests/conftest.py:288-295`): a registered route never reached FAILS the test.
14. Three conditional expressions live in `:80-229` — `:129`, `:185`, `:187` — by AST walk.

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `tests/test_shared_tasks_send_reply.py` | **Create.** The whole sub-project's tests: prelude then four clusters in source order. | 1, 3-8 |
| `app/shared/tasks/notes.py` | **Modify, at most one line.** The guard Task 2's measurement selects. | 2 |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Append.** D309 onward. | 9 |
| `tests/README.md` | **Append.** New harness facts. | 9 |
| `coverage_floors.ini` | **Add one line.** `app/shared/tasks/notes.py` has no entry. | 9 |

---

### Task 1: The file, its prelude, and the mention scan

**Files:**
- Create: `tests/test_shared_tasks_send_reply.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the prelude every later task uses —
  - `_seed(body='a reply', with_parent_reply=False, local_community=True, with_keys=False)` → `SimpleNamespace(instance, user, community, post, reply, parent)`. Seeds the LOCAL instance first. `parent` is a `PostReply` when `with_parent_reply=True`, else the `Post`.
  - `_peer(domain='peer.example', software='lemmy')` → `Instance`, always called **after** `_seed()`.
  - `_send(s, edit=False)` → `send_reply(s.reply.id, s.parent.id if isinstance(s.parent, PostReply) else None, edit=edit, session=db.session)`.
  - `_remote_inbox(s, http_mock, inbox=PEER_INBOX)` and `_sent_activity(route, index=-1)` — the capture pair, ported from `tests/test_shared_tasks_send_post.py`.
  - An **autouse** `socket.getaddrinfo` stub for `.example` hosts.

- [ ] **Step 1: Read the sub-project 19 harness before writing anything**

Open `tests/test_shared_tasks_send_post.py` and read its prelude — the module docstring, `_seed`, `_peer`, `_remote_inbox`, `_sent_activity`, and the autouse `socket.getaddrinfo` fixture. **Port them; do not reinvent them.** In particular the DNS stub must keep its delegating shape (canned answers only for `example` and `*.example`, every other host passed to the real resolver) and its docstring explaining why it must not be deleted.

Say in your report which helpers you ported verbatim and which you had to change, and why.

- [ ] **Step 2: Create the file with its module docstring and prelude**

```python
"""`send_reply` -- the Celery-path builder and deliverer of an ActivityPub Note.

`app/shared/tasks/notes.py:80-229`. This is the TWIN of `send_post`
(`app/shared/tasks/pages.py:88-352`), which sub-project 19 took to two
unreachable statements and three unreachable arms. The two functions share a
mention scan, early returns, a Create/Announce construction and a per-instance
fan-out -- so the DIFFERENCES between them are this sub-project's findings.

FIVE DIVERGENCES, established before any test was written:

1. `:92` scans `reply.body` with `re.finditer` and NO guard, where
   `pages.py:97` guards its scan with `if post.body:`. `PostReply.body` is
   `db.Column(db.Text)` (`app/models.py:2901`), so None is storable. Whether a
   None-bodied reply can REACH this line is measured in this file, not assumed.

2. `:143` does not test `community.private`, and `pages.py:153` does. That flag
   is commented "only members can view. no federation" (`app/models.py:611`),
   and `pages.py` is the only one of TEN senders in `app/shared/tasks/` that
   honours it. The leak is LATENT: both writers of the flag couple it to
   `local_only` in the view layer (`app/community/routes.py:103-104` and
   `:1230-1231`), so no current path produces `private=True, local_only=False`,
   and `:143`'s `local_only` test already catches every private community that
   exists.

3. `:217`'s `instance.online()` cannot be False. `:216` calls
   `community.following_instances()` with the default `include_dormant=False`,
   so `app/models.py:849-850` already filters `dormant` and `gone_forever` in
   SQL, and `Instance.online()` (`:118-119`) is exactly
   `not (dormant or gone_forever)`.

4. `:203`/`:225` repeat an `@context` del/re-add pair sub-project 19 proved
   yields an EQUIVALENT MUTANT: `post_request`
   (`app/activitypub/signature.py:100-101`) re-adds the key when a body lacks
   it, same value and same trailing position, before serialization. Tests here
   assert KEY ORDER and say so rather than claiming a kill.

5. There is NO amendment block. `send_post` rewrites its Page into a Note at
   `:309-330`, which is where its aliasing hazard lives; this function is
   already a Note. Assertions still read serialized bytes, because `:203`'s
   `del` mutates `create` in place.

ENTRY. `send_reply(reply_id, parent_id, edit=False, session=None)`. `session`
has no usable default -- `:81` dereferences it immediately. `parent_id` is a
BRANCH, not a convenience: truthy loads a PostReply as the parent (`:84`),
falsy uses `reply.post` (`:86`), and the choice changes what `:175`'s
`parent.public_url()` emits.

RECIPIENTS START SEEDED. `:90` is `recipients = [parent.author]`, where
`send_post` starts from `[]`. Then `:120` is
`if recipient.is_local() and recipient.id != parent.author.id:` -- so the
parent's author is always a delivery recipient but is deliberately excluded
from the mention notification. That interaction has no analogue in the twin.

THREE EARLY RETURNS stand between entry and the builder: `:143-144`,
`:147-148`, `:149-151`. `send_post` has a fourth only because it tests
`community.private`.
"""
```

Then port the prelude helpers from `tests/test_shared_tasks_send_post.py`, adapting `_seed` to build a `PostReply`:

```python
def _seed(body='a reply', with_parent_reply=False, local_community=True, with_keys=False):
    """instance, author, community, post, reply -- and the parent the call needs.

    ORDER IS LOAD-BEARING. `make_community` hardcodes `instance_id=1` and
    tests/conftest.py:143 truncates with RESTART IDENTITY, so the local instance
    is created first; a peer built before this call would take id 1 and leave
    the community's FK pointing at it.

    `with_parent_reply=True` makes the parent a PostReply, which is `:83`'s
    true arm; the default leaves it the Post, which is `:86`. `make_post_reply`
    (tests/factories.py:453) does not set `parent_id` or `depth`, so a nested
    reply sets them here.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795-796) is a DISJUNCTION and
    `ap_id` alone leaves it local.
    """
```

Give it the same shape `tests/test_shared_tasks_send_post.py`'s `_seed` has, returning `SimpleNamespace(instance, user, community, post, reply, parent)`.

- [ ] **Step 3: Verify the file collects**

Run: `./run_tests.sh tests/test_shared_tasks_send_reply.py -q`
Expected: `no tests ran`. An `ImportError` here is a real defect — confirm every imported name exists and drop any that does not, reporting what you dropped.

- [ ] **Step 4: Commit the prelude**

```bash
git add tests/test_shared_tasks_send_reply.py
git commit -F <message-file>
```

Subject: `test: open tests/test_shared_tasks_send_reply.py with the send_reply harness`

- [ ] **Step 5: Write the entry and parent-dispatch tests**

**These two tests need a real observable, and `Notification.query.count() == 0` is not one.** Absence of a notification holds whether or not `:83` picked the right parent, so a test asserting only that would pass against a broken dispatch. The arm's real witness is `inReplyTo` at `:175`, which is `parent.public_url()` — a `Post` URL for the false arm and a `PostReply` URL for the true one. You ported the capture pair in Step 1; use it here.

```python
def test_a_top_level_reply_names_the_post_as_its_inReplyTo(db_session, http_mock):
    """:83's FALSE arm and :86. `parent_id` is None, so `parent = reply.post`
    and `:175`'s `inReplyTo` is the POST's public url.

    The delivered body is the witness. Asserting only that no notification
    appeared would pass against a broken dispatch, because the post's author is
    excluded at :120 either way.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert _sent_activity(route)['object']['inReplyTo'] == s.post.public_url()


def test_a_nested_reply_names_its_parent_reply_as_its_inReplyTo(db_session, http_mock):
    """:83's TRUE arm and :84. `parent_id` is set, so the parent is a PostReply
    and `:175`'s `inReplyTo` is the PARENT REPLY's public url -- a different
    value from the test above, which is what makes the pair discriminating."""
    s = _seed(with_parent_reply=True, local_community=False, with_keys=True)
    assert isinstance(s.parent, PostReply)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert _sent_activity(route)['object']['inReplyTo'] == s.parent.public_url()
    assert s.parent.public_url() != s.post.public_url()
```

- [ ] **Step 6: Write the mention-scan tests**

Cover, one named test per arm, following the shapes in `tests/test_shared_tasks_send_post.py`'s mention cluster:

| Line | Arm |
|---|---|
| `:93` | a body with no `@name@host` match, so the loop body never runs |
| `:95` | the local-host arm (`match.group(2) == SERVER_NAME`) and the remote arm |
| `:97` | the author mentioning themselves, which skips `search_for_user` entirely |
| `:100-101` | the local `except: pass`. **`search_for_user` cannot raise for a bare local name** — `app/user/utils.py:88` finds no `@`, `:91-92` sets `server = ''`, `:94` is False so the only `raise` at `:98` is skipped, and `:108-109` returns `None` cleanly. Sub-project 19 established this. Do not write a test claiming to reach it; note it for Task 8. |
| `:104-107` | the remote `except: pass`, which **is** reachable — seed a `BannedInstances` row for the mentioned host so `app/user/utils.py:98` raises |
| `:108` | a mention that resolves and one that does not |
| `:110-116` | the dedup, both `add_recipient` arms |

Note that `:90` seeds `recipients` with the parent's author, so the dedup loop at `:110` runs against a non-empty list from the first iteration — unlike `send_post`, where the first mention always appends. A test for the dedup must therefore choose whether it is deduping against the parent author or against an earlier mention, and say which.

- [ ] **Step 7: Run and commit**

Run: `./run_tests.sh tests/test_shared_tasks_send_reply.py -q`

```bash
git add tests/test_shared_tasks_send_reply.py
git commit -F <message-file>
```

Subject: `test: cover send_reply's entry, parent dispatch and mention scan`

---

### Task 2: The `reply.body` reachability measurement, and at most one fix

**Files:**
- Modify: `tests/test_shared_tasks_send_reply.py` (append)
- Modify: `app/shared/tasks/notes.py` — **at most one line, and only if the measurement warrants it**

**This task decides the sub-project's one production change. Measure before you fix.**

- [ ] **Step 1: Establish whether a `None`-bodied reply can reach `:92`**

`:92` is `matches = re.finditer(pattern, reply.body)` with no guard, where `app/shared/tasks/pages.py:97` guards its equivalent with `if post.body:`. `PostReply.body` is `db.Column(db.Text)` (`app/models.py:2901`) — nullable.

Trace both writers and report what you find:
- The local path: `app/shared/reply.py:182` passes `body=piefed_markdown_to_lemmy_markdown(content)` to `PostReply.new`. Read `piefed_markdown_to_lemmy_markdown` and decide whether it can return `None`, and whether anything upstream rejects an empty `content` first.
- The federated path: `PostReply.new` (`app/models.py:2995`). Read what it does with an inbound Note carrying no content.

**Report the answer with the evidence, before writing any test.**

- [ ] **Step 2: Write the probe test**

Regardless of the answer, write a test that seeds a `None` body directly and calls `send_reply`:

```python
def test_a_reply_with_no_body_does_not_crash_the_mention_scan(db_session):
    """:92 scans `reply.body` with no guard, where `pages.py:97` guards its
    equivalent with `if post.body:`. `PostReply.body` is nullable
    (`app/models.py:2901`).

    THE DOCSTRING'S SEVERITY CLAIM IS WRITTEN AFTER THE MEASUREMENT, NOT
    BEFORE. Step 1 establishes whether a None body can reach this line through
    a production path; this test only establishes what happens when it does.
    """
    s = _seed()
    s.reply.body = None
    db.session.commit()

    _send(s)
```

- [ ] **Step 3: Run it and record the exact failure**

Run: `./run_tests.sh tests/test_shared_tasks_send_reply.py -q -k no_body`
Expected: FAIL with `TypeError: expected string or bytes-like object, got 'NoneType'`.

Quote the exact line. **If it fails with anything else, stop and report** — a different failure means the test does not reach `:92`.

- [ ] **Step 4: Decide the fix, and land at most one**

Two outcomes, and the plan authorises exactly one production change either way:

- **If Step 1 found a production path** that produces a `None` body: guard `:92` to match `pages.py:97`. The failing test above is its proof.
- **If Step 1 found none**: the crash is latent. Do **not** guard `:92` — a guard against an unreachable state is untestable defence whose mutants nobody can kill. Instead land the private gate at `:143`, making it `if community.local_only or community.private or not community.instance.online():`, on the equivalence footing the spec's §2.2 describes, and keep the probe test with its docstring recording the latency.

State which branch you took and why. Then commit the fix **before** running mutations.

- [ ] **Step 5: Mutation-prove whichever guard landed**

For a `:92` guard: `if True:`, `if False:`, and reverting it. For a `:143` gate: mutate the new conjunct alone to `True` and to `False`, leaving the other two conjuncts intact, and confirm each dies by a distinct named test.

After **every** mutation: `git checkout -- app/`, then confirm **both** `git diff -- app/` empty and `wc -l app/shared/tasks/notes.py` = `310`.

- [ ] **Step 6: Commit**

```bash
git add app/shared/tasks/notes.py tests/test_shared_tasks_send_reply.py
git commit -F <message-file>
```

The message body must state which branch of Step 4 you took, the evidence for it, and — if you landed the private gate — that the other eight senders stay registered.

---

**A note on Tasks 3 through 6, which specify tests by line and arm rather than by full code.** Their assertions all read a captured outbound request, and the capture helpers are whatever Task 1 ported from `tests/test_shared_tasks_send_post.py`. Writing the bodies out here would hard-code a shape Task 1 has not yet fixed. Read Task 1's committed tests first and follow them exactly; do not introduce a second capture mechanism. Every test still needs its own docstring naming the production line and the arm it takes.

---

### Task 3: The mention-notification block

**Files:**
- Modify: `tests/test_shared_tasks_send_reply.py` (append)

Target: `:118-141`.

- [ ] **Step 1: Write the notification tests**

One named test per arm, each asserting on a real `Notification` row:

| Line | Arm |
|---|---|
| `:120` first conjunct | a remote recipient, who gets no notification |
| `:120` second conjunct | **the parent's author, who is seeded into `recipients` at `:90` and excluded here** — this is the divergence from `send_post` and needs its own test |
| `:121-124` | `edit=True` with an existing notification, and `edit=False` |
| `:125` | the `not existing_notification` false arm — a second `edit=True` call after the first created one |
| `:127-132` | the `targets_data` dict, asserting `post_id`, `comment_id` and `comment_body` |
| `:129` | **both arms of the conditional expression** — an author with `ap_id` and one without. coverage.py emits no arc for a ternary (fact 87). |
| `:139` | `unread_notifications` incremented |

The `:120` second-conjunct test is the important one. `send_post` has no equivalent, because its `recipients` starts empty. Assert that a mention of the parent's author produces **no** notification while a mention of a third party does.

- [ ] **Step 2: Run and commit**

Subject: `test: cover send_reply's mention notifications and the parent-author exclusion`

---

### Task 4: The three early returns

**Files:**
- Modify: `tests/test_shared_tasks_send_reply.py` (append)

Target: `:143-151`.

- [ ] **Step 1: Write one test per return, each proving execution reached it**

Each test must prove **both** that the return happened and that execution got that far. The shape sub-project 19 used: assert that the mention notification from `:118-141` landed while nothing after the return ran. Confirm that assertion actually discriminates — a test that returns at `:144` while claiming to test `:148` proves nothing, and sub-project 19 shipped exactly that defect until a reviewer caught it.

| Test | Arm | Setup |
|---|---|---|
| local-only community | `:143` first disjunct | `community.local_only = True` |
| dormant instance | `:143` second disjunct | `s.instance.dormant = True` |
| gone-forever instance | `:143` second disjunct, other column | `s.instance.gone_forever = True` |
| banned author | `:147-148` | a `CommunityBan` row for `(author, community)` |
| remote community, blocked instance | `:149-151` first disjunct | `_seed(local_community=False)` plus `make_instance_block` |
| remote community, banned instance | `:149-151` second disjunct | a `BannedInstances` row for the community's host |
| local community skips `:149` | `:149` false arm | the default seeding |

**Assert `s.community.is_local() is False` immediately after seeding** in the two remote-community tests. Sub-project 19 shipped a test whose `:159` guard never opened because `Community.is_local()` is a disjunction and only `ap_id` had been set; that assertion is what would have caught it.

If a test cannot discriminate the early return from full completion, find an observable that does — sub-project 19 used `ActivityPubLog.query.count() == 0`, because a run reaching delivery with an unset `ap_inbox_url` writes a failure row unconditionally at `app/activitypub/signature.py:98-105`. Say which observable you used.

- [ ] **Step 2: Run and commit**

Subject: `test: cover send_reply's three early returns`

---

### Task 5: The Note and Create builders

**Files:**
- Modify: `tests/test_shared_tasks_send_reply.py` (append)

Target: `:153-199`, 18 statements and only 4 branch arms — the region is mostly a flat dict literal, so most of it is covered by any test that reaches it and asserts on the delivered body.

- [ ] **Step 1: Assert the Note's shape on the wire**

Use the capture pair. One test asserting the full key set of `activity['object']`, and named assertions for the fields that are computed rather than copied:

| Line | What to pin |
|---|---|
| `:156-158` | both arms — a reply with a mention (so `tag` and `cc` gain entries) and one without |
| `:159` | `reply.tags_for_activitypub()` extending `tag` |
| `:175` | `inReplyTo` — **different for a nested reply than a top-level one**, which is `:83`'s branch showing up in the output |
| `:176` | `published` from `ap_datetime(reply.posted_at)` |
| `:179`, `:180` | `distinguished` and `flair` |
| `:182-183` | `updated` present when `edit=True`, absent when `edit=False` |
| `:185`, `:187` | **both arms of each conditional expression** — the activity id contains `create` or `update`, and `type` is `Create` or `Update` |

`:185` and `:187` are ternaries, invisible to coverage (fact 87), and they are observable: `:186` builds the id from `activity`, and `:190` carries `type`. Assert both.

- [ ] **Step 2: Run and commit**

Subject: `test: cover send_reply's Note and Create builders`

---

### Task 6: Announce construction and delivery

**Files:**
- Modify: `tests/test_shared_tasks_send_reply.py` (append)

Target: `:201-229`.

- [ ] **Step 1: Cover both delivery shapes**

| Line | Arm |
|---|---|
| `:202` | `community.is_local()` true — the Announce path at `:203-219` — and false, the direct Create at `:221-222` |
| `:203` + `:225` | the `@context` del and re-add. **Assert key order**, per harness fact 8; do not claim a kill on `:226`. |
| `:216-219` | a following instance that receives the Announce; assert the request is signed with the **community's** key id, not the user's |
| `:217` | its first conjunct (`instance.inbox`) and its third and fourth (`has_blocked_instance`, `instance_banned`). **Not the second** — `instance.online()` is unreachable-False, per harness fact 7. |
| `:227-229` | a mentioned recipient on an instance not already in `domains_sent_to`, and one that is |

For the `:217` skip cases, note that deleting `instance.inbox and` yields an "empty uri" `ActivityPubLog` row and no HTTP at all, so a call-count assertion passes against that mutant. Sub-project 19 used `ActivityPubLog.query.count()` instead; do the same and say so.

- [ ] **Step 2: Run and commit**

Subject: `test: cover send_reply's Announce construction and delivery`

---

### Task 7: Residuals and the AST walk

**Files:**
- Modify: `tests/test_shared_tasks_send_reply.py` (append whatever the measurement demands)

- [ ] **Step 1: Ask the controller for the scoped measurement**

Do NOT run the full suite. Report ready and ask for the missing statements and branch arms within `app/shared/tasks/notes.py:80-229`. Starting point was 86 statements and 48 branch arms.

- [ ] **Step 2: Write a test for each remaining reachable arm**

One named test per arm, docstring naming the line and which arm it takes.

**Two are expected to remain and must NOT be chased:**
- `:100-101`, the local-arm `except: pass` — `search_for_user` cannot raise for a bare local name (harness fact in the module docstring).
- `:217`'s `instance.online()` conjunct — already filtered in SQL.

If any other arm is unreachable, write the argument rather than a test, consult `tests/README.md` fact 75's catalogue of causes, and **name the establisher** — an earlier return, an unconditional assignment, a callee that cannot raise, or a query that already filtered. Fact 75 was extended in sub-project 19 to require exactly that.

- [ ] **Step 3: Run the AST walk and reconcile**

```bash
python3 - <<'PY'
import ast
src = open('app/shared/tasks/notes.py').read()
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.FunctionDef) and node.name == 'send_reply':
        for sub in ast.walk(node):
            if isinstance(sub, ast.IfExp):
                print(sub.lineno, ast.get_source_segment(src, sub))
PY
```

Three are expected — `:129`, `:185`, `:187` — but **report the walk's raw output rather than confirming the expectation**. Sub-project 19's controller named three ternaries where the walk found eight. Reconcile every result against a named test taking each arm.

- [ ] **Step 4: Write the unreachability comment block, then commit**

Add a comment block naming every unreachable item with its establisher, in the file where the arms live. Re-derive every line number in it before committing — this is committed prose a later sub-project will trust.

Subject: `test: close send_reply's residual arms and record the unreachable ones`

---

### Task 8: Register the findings, raise the floor, verify

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Establish the next free finding number**

```bash
grep -o 'D[0-9]\{3\}' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u | tail -5
```

Sub-project 19 ended at **D308**, so the first new number is **D309**. Confirm against the output — sub-project 18 used the wrong number throughout for want of this check, and the register carries a note about it.

- [ ] **Step 2: Append the sub-project 20 section**

Read sub-project 19's section first and match its structure. Content:

1. **Whichever guard landed**, with the Step 1 evidence that decided it and the mutation results.
2. **The private-gate asymmetry, recorded once.** `Community.private` (`app/models.py:611`) is commented "only members can view. no federation"; `app/shared/tasks/pages.py:153` is the only one of ten senders that tests it; the nine omitting sites are `notes.py:143`, `notes.py:248`, `adds.py:63`, `blocks.py:104`, `flags.py:57`, `groups.py:59`, `likes.py:60`, `locks.py:89`, `removes.py:63` — **re-derive every one**. State the severity as **latent** and name the reason: both writers couple the flag to `local_only` in the view layer (`app/community/routes.py:103-104` and `:1230-1231`), so no current path produces the uncoupled state.
3. **`:217`'s redundant `online()` conjunct**, as a fourth instance of the shape sub-project 19 registered with one structural cause — the query filters, the loop re-checks.
4. **The `reply.body` scan**, with its measured reachability and severity.
5. Anything Task 7 found.

Do a two-pass citation sweep (fact 100), and grep for the value being corrected rather than the text being edited.

- [ ] **Step 3: Add the harness facts**

```bash
grep -o '^1[0-9][0-9]\.' tests/README.md | sort -u | tail -3
```

Sub-project 19 ended at fact 113 plus extensions to 75, 89 and 100. Append from 114, and check whether each candidate is already carried. Candidates, each written only if it bit:
- `send_reply`'s `parent_id` is a branch selecting a `PostReply` or the `Post`, and the choice shows up in `inReplyTo`.
- `recipients` starts seeded with the parent's author, so the dedup loop runs against a non-empty list from the first iteration, and the parent author is excluded from notification while remaining a delivery target.
- Whatever the `reply.body` measurement established.

- [ ] **Step 4: Ask the controller for the blended figure and add the floor**

`app/shared/tasks/notes.py` has **no entry** in `coverage_floors.ini`; it was 10.0457% before this work. Ask for the post-work blended `summary['percent_covered']`, then add `app/shared/tasks/notes.py = <measured, floored to a whole percent>` in the file's existing ordering.

- [ ] **Step 5: Verify the floor check**

```bash
python3 tests/check_coverage_floors.py scratch_full_cov.json coverage_floors.ini
```

Expect exit 0 with the new module listed. **Read the report mtime it prints** — the file is gitignored and persists, so a run that failed to write it leaves stale data the ratchet would pass against.

- [ ] **Step 6: Ask the controller for the full-suite run, then commit**

Report ready. The controller runs the suite once, **in the foreground**, and reports counts, wall time and the report mtime.

Subject: `docs: register sub-project 20's findings and set app/shared/tasks/notes.py's first floor`

---

## Success criteria

From spec §9:

1. `tests/test_shared_tasks_send_reply.py` exists and calls `send_reply` directly.
2. `send_reply` (`:80-229`) is at zero uncovered statements and zero uncovered branch arms, except any proved unreachable with a written argument naming its establisher.
3. Both arms of `:83`'s `parent_id` test are exercised, and the `recipients = [parent.author]` seeding's interaction with `:120`'s exclusion is covered by named tests.
4. All three conditional expressions — `:129`, `:185`, `:187` — have both arms exercised by named tests, reconciled by an AST walk rather than by the coverage number.
5. The `reply.body` reachability is established by execution and the finding's severity recorded accordingly.
6. At most one production guard lands, proved by a test that fails before it.
7. The register carries D309 onward, with the private-gate asymmetry recorded once across all nine omitting sites and its severity stated as latent.
8. `coverage_floors.ini` gains an `app/shared/tasks/notes.py` entry at the measured floor.
9. The full suite is green, run in the foreground, with the pass/skip counts and the coverage report's mtime recorded.

# Inbox Moderation Arms 5c Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `process_inbox_request`'s `Delete`, `Lock`, `Add`, `Remove` and `Block` arms — 263 statements, 5 executed — to full statement and branch coverage with mutation evidence per guard, fix the six live defects the spec names, and register everything else found.

**Architecture:** Tests call `dispatch(activity, store_ap_json=True)` from `tests/test_inbox_dispatch_preamble.py`, which invokes `process_inbox_request` directly — production's DEBUG branch at `routes.py:758-759`. `Add`, `Remove` and `Block` take `community`/`feed` from the preamble, so a test selects a branch by choosing the activity's **actor**; `Delete` and `Lock` resolve their own targets from `core_activity['object']`. Outbound sends and moderation delegates are doubled at their binding site on the routes module.

**Tech Stack:** pytest, `podman-compose` via `./run_tests.sh`, `coverage.py` (branch mode), fakeredis, respx.

**Spec:** `docs/superpowers/specs/2026-08-30-coverage-inbox-moderation-5c-design.md`

## Global Constraints

- **`if TYPE_CHECKING` is always a bug.** Never introduce it.
- **Imports go at the top of the file. No new inline imports.** (`routes.py:1417` already has one; it is registered, not fixed.)
- **Findings are registered, not fixed — with exactly six exceptions**, Tasks 2, 4, 6 and 8, which the spec authorises by name. No other defect is fixed in this sub-project.
- **Floors only ever rise.** Current floor for `app/activitypub/routes.py` is 35.
- **ONE test run at a time in this checkout.** Two concurrent runs deadlock on the shared tmpfs database and hang indefinitely — this cost sub-project 5b nearly two hours. Never start a run while another is going.
- **Never background a test run whose result you need.** Run it in the foreground and wait for it to print. Three implementers in 5b stalled waiting on monitors that died with them.
- **Never `./run_tests.sh --down`** — it destroys the tmpfs database and replays ~269 migrations.
- **`--cov=app.module` (dotted) works; `--cov=app/module.py` (path) silently measures nothing.**
- **Fix tasks run the FULL suite; coverage-only tasks run only their own file.**
- **A guard is tested on the whole domain it claims to reject**, and each half of a compound guard is dropped separately and must die distinctly.
- **A mutant killed by `respx.models.AllMockedAssertionError` is an INFRASTRUCTURE kill, not a behavioural one** — it fires inside a blocked fetch before any assertion runs. Re-run such a mutant with the fetch served (`federation_peer` / `http_mock`, `tests/conftest.py:288` and `:299`). A mutant that survives once the fetch succeeds is a legitimate finding — record it; never contrive a test to hide it.
- **Beware default-backed assertions.** 5b shipped one: `community.last_active is not None`, against a column declared `default=utcnow`, so it was already non-None before dispatch. Before asserting a column's value proves a write, check its declared default in `app/models.py` and seed a distinguishable starting value.
- **Tests asserting an exception use `pytest.raises` with a specific `match=`.** A bare `Exception` catch is vacuous.
- **`LOG_ACTIVITYPUB_TO_DB` is False by default**; asserting on `ActivityPubLog` requires `monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)` (`app/activitypub/util.py:4527`). An `ActivityPubLog.query.count() == 0` assertion with logging OFF is vacuous.
- **Seed actors with `ap_fetched_at = utcnow()` stamped**, or `find_actor_or_create_cached` calls `schedule_actor_refresh`, firing a real actor fetch inline under eager Celery.
- `tests/test_activitypub_util.py` (3 tests) needs live network and is expected to SKIP.
- Baseline at `bfee4a4e`: **683/1814 statements, 284/892 branches, 35.735 % blended, floor 35.** Full suite **2868 passed, 3 skipped**.
- Commit trailer: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

## File structure

| File | Responsibility |
|---|---|
| `tests/factories.py` (modify) | `make_feed_item`, `make_feed_member` |
| `tests/test_inbox_dispatch_lock_delete.py` (create) | Tasks 1–5: `Lock` and `Delete`, plus Fixes 1–3's regression tests |
| `tests/test_inbox_dispatch_add_remove.py` (create) | Tasks 6–10: `Add` and `Remove`, plus Fixes 4–6's regression tests |
| `tests/test_inbox_dispatch_block.py` (create) | Task 11: `Block` |
| `app/activitypub/routes.py` (modify, Tasks 2, 4, 6, 8 ONLY) | the six authorised fixes |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | Task 12: the register, continuing from D80 |
| `coverage_floors.ini`, `tests/README.md` (modify) | Task 13: the raised floor and this slice's harness facts |

`Add` and `Remove` share a file because they are near-mirrors over the same feed-and-community fixtures, and reading them together is what makes the `APLOG_ADD` mislabelling visible. `Lock` and `Delete` share one because both resolve their own targets and both need post/reply fixtures. `Block` shares nothing with either.

### The shared moderation recorder, defined once in Task 1

```python
def record_moderation(monkeypatch, *names):
    """Double moderation delegates at their binding site on the routes module.

    routes.py imports each of these by name, so patching the defining module
    would leave routes' copy pointing at the original — the binding-site trap
    tests/conftest.py:394 documents at length.

    Returns {name: [call_args_tuple, ...]}.
    """
    calls = {}
    for name in names:
        calls[name] = []
        monkeypatch.setattr(
            activitypub_routes, name,
            lambda *a, _n=name, **kw: calls[_n].append((a, kw)))
    return calls
```

Tasks 3, 5, 7, 9, 10 and 11 import it from `tests/test_inbox_dispatch_lock_delete.py`.

---

### Task 1: Fixtures and the two outcome tables

**Files:**
- Modify: `tests/factories.py`
- Create: `tests/test_inbox_dispatch_lock_delete.py`

**Interfaces:**
- Produces: `make_feed_item(feed, community)`, `make_feed_member(user, feed, is_owner=False)`, and `record_moderation(monkeypatch, *names)`.

- [ ] **Step 1: Add the two factories to `tests/factories.py`**

`FeedItem` and `FeedMember` are both hand-built inline in 5b's files; three arms in this slice need them.

```python
def make_feed_item(feed: Feed, community: Community) -> FeedItem:
    """One community's membership of a feed. Add creates these; Remove deletes them."""
    item = FeedItem(feed_id=feed.id, community_id=community.id)
    db.session.add(item)
    db.session.commit()
    return item


def make_feed_member(user: User, feed: Feed, is_owner: bool = False) -> FeedMember:
    """A user's subscription to a feed.

    `is_owner` and `is_banned` both default to False on the model
    (app/models.py:4034-4035), which is what Feed.subscribed() reads to
    return SUBSCRIPTION_MEMBER rather than OWNER or BANNED.
    """
    member = FeedMember(feed_id=feed.id, user_id=user.id, is_owner=is_owner)
    db.session.add(member)
    db.session.commit()
    return member
```

Add `FeedItem` and `FeedMember` to the module's top-level model imports if they are not already there.

- [ ] **Step 2: Create the test file with the recorder and both outcome tables**

Create `tests/test_inbox_dispatch_lock_delete.py` containing `record_moderation` exactly as given in the File structure section above, plus a module docstring holding outcome tables you **derive from source yourself** for `Lock` (`routes.py:1356-1395`) and `Delete` (`:1264-1327`).

Record per branch: what selects it, what it writes, what it delegates to, and what it logs — **including the paths that log nothing**. Do not copy the spec's prose. Every enumeration in this campaign has been wrong somewhere on re-derivation; finding that is the point. If what you derive disagrees with the spec, trust the source and say so in your report.

- [ ] **Step 3: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_lock_delete.py -q && \
  git add tests/factories.py tests/test_inbox_dispatch_lock_delete.py && \
  git commit -m "test: add the feed-item and feed-member fixtures the moderation arms need

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: FIX 1 — Lock's comment branch cannot complete

**Files:**
- Modify: `tests/test_inbox_dispatch_lock_delete.py`
- Modify: `app/activitypub/routes.py:1380, 1386, 1387` **(authorised fix — three identifiers)**

**This is one of four tasks permitted to change `app/`.**

- [ ] **Step 1: Write two failing tests**

Inside `elif post_reply:` (`routes.py:1379`), `post` is guaranteed `None`, yet `:1380` reads `post.community`, and `:1386-1387` read `post.author` and `post.community`. Both outcomes of the branch therefore raise.

```python
def test_a_moderator_can_lock_a_comment(app, db_session, monkeypatch):
    """routes.py:1379-1388. Pre-fix this raises AttributeError at :1386
    (`target_user=post.author`, with post None) on the SUCCESS path.

    Asserts the corrected behaviour: replies_enabled goes False on the reply
    AND on its subtree via the raw UPDATE at :1382-1385, add_to_modlog is
    called with the reply's own author and community, and SUCCESS is logged.
    """


def test_a_non_moderator_locking_a_comment_is_refused(app, db_session, monkeypatch):
    """routes.py:1380. `post_reply.community.is_moderator(mod)` is False, so
    the `or` does NOT short-circuit and `post.community` is evaluated with
    post None — turning the permission refusal into an AttributeError.

    Asserts the corrected behaviour: 'Lock: Does not have permission' logged,
    replies_enabled unchanged.
    """
```

Seed a `PostReply` whose `ap_id` contains `/comment/` so `:1362` selects it, and give it at least one child reply so the subtree update is observable.

- [ ] **Step 2: Run them and record both failures**

```bash
./run_tests.sh tests/test_inbox_dispatch_lock_delete.py -v
```

Expected: both FAIL with `AttributeError: 'NoneType' object has no attribute ...`. Record the exact text of each. A test that only passes after a fix is not evidence the fix did anything.

- [ ] **Step 3: Apply the fix**

Three identifiers in `app/activitypub/routes.py`:

```python
# :1380
-                        if post_reply.community.is_moderator(mod) or post.community.is_instance_admin(mod):
+                        if post_reply.community.is_moderator(mod) or post_reply.community.is_instance_admin(mod):
# :1386
-                            add_to_modlog('lock_post_reply', actor=mod, target_user=post.author, reason=reason,
+                            add_to_modlog('lock_post_reply', actor=mod, target_user=post_reply.author, reason=reason,
# :1387
-                                          community=post.community, reply=post_reply,
+                                          community=post_reply.community, reply=post_reply,
```

- [ ] **Step 4: Confirm they pass, then run the WHOLE suite**

```bash
./run_tests.sh -q
```

Foreground, once. Expected: 2868 passed, 3 skipped, plus your two. **If anything else fails, that is a finding about the existing tests — report it, do NOT amend them.**

- [ ] **Step 5: Commit the fix on its own**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_lock_delete.py
git commit -m "fix(activitypub): make Lock's comment branch reference the reply, not the post

Inside `elif post_reply:` the post variable is always None -- the branch is
only reached when the `if post:` above it was falsy -- but three statements
dereferenced it. The permission check at :1380 raised whenever the actor was
not a moderator, and :1386-1387 raised on the success path, so federated
comment locking failed on every outcome. PostReply carries both author and
community relationships (app/models.py:2899-2900).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The rest of the Lock arm

**Files:**
- Modify: `tests/test_inbox_dispatch_lock_delete.py`

`routes.py:1356-1395`, now that the comment branch works.

- [ ] **Step 1: Cover the three target-resolution paths**

`:1360-1366` picks the target by substring: `/post/` → `Post.get_by_ap_id`; `/comment/` → `PostReply.get_by_ap_id`; neither → try `Post` first, then `PostReply` on `None`. Three tests, plus one for the fallback's second half (an object URL containing neither substring that resolves to a reply).

- [ ] **Step 2: Cover the post branch's two outcomes**

`:1369-1378`: a moderator locks the post (`comments_enabled` False, `add_to_modlog('lock_post', ...)`, SUCCESS), and a non-moderator is refused (`'Lock: Does not have permission'`). Assert the modlog call's `target_user`, `community` and `post` arguments, not merely that it was called.

- [ ] **Step 3: Cover the not-found outcome**

`:1393-1394`, `'Lock: post not found'`.

- [ ] **Step 4: Mutate both permission guards**

`post.community.is_moderator(mod) or post.community.is_instance_admin(mod)` (`:1370`) and the corrected `post_reply` equivalent (`:1380`) are each two alternatives. Drop each half separately; each must be killed by a **distinct** test, which means seeding a moderator who is not an instance admin, and an instance admin who is not a moderator. Report each mutant and which KIND of kill it produced.

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_lock_delete.py -q && \
  git add tests/test_inbox_dispatch_lock_delete.py && \
  git commit -m "test: cover the Lock arm's target resolution and both permission guards

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: FIX 2 and FIX 3 — Delete's unguarded feed and actor

**Files:**
- Modify: `tests/test_inbox_dispatch_lock_delete.py`
- Modify: `app/activitypub/routes.py:1268-1279` **(authorised fix)**

- [ ] **Step 1: Write two failing tests**

```python
def test_a_delete_naming_an_unknown_feed_is_refused(app, db_session, monkeypatch):
    """routes.py:1270-1279. The feed lookup returns None, and :1273 reads
    feed.user_id before the `if feed:` guard at :1278 -- so the else branch
    at :1300 written to log 'Delete: cannot find ...' is unreachable and an
    AttributeError escapes instead.

    Asserts the corrected behaviour: the not-found failure is logged.
    """


def test_a_delete_from_an_unresolvable_actor_is_refused(app, db_session, monkeypatch):
    """routes.py:1268 and :1273. find_actor_or_create_cached can return None
    -- validate_remote_actor refuses banned and malformed actors -- and
    :1273 then reads user.id.

    Asserts the corrected behaviour: a logged failure rather than a crash.
    """
```

- [ ] **Step 2: Run them and record both failures**

Expected: both FAIL with `AttributeError`. Record the exact text.

- [ ] **Step 3: Apply the fix**

Reorder so both refusals precede the ownership check, and collapse the now-redundant `if feed:` / `else:`:

```python
                        user = find_actor_or_create_cached(actor_id)
                        feed = session.query(Feed).filter_by(ap_public_url=core_activity['object']['id']).first()

                        if not user:
                            log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, saved_json,
                                            'Delete rejected, could not find the sender.')
                            return
                        if not feed:
                            log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, saved_json,
                                            f"Delete: cannot find {core_activity['object']['id']}")
                            return

                        # make sure the user sending the delete owns the feed
                        if not user.id == feed.user_id:
                            log_incoming_ap(id, APLOG_DELETE, APLOG_FAILURE, saved_json, 'Delete rejected, request came from non-owner.')
                            return
```

Then de-indent the deletion body that followed `if feed:` by one level and drop the `else:` clause, whose message the new guard now emits. **Preserve the deletion body exactly otherwise** — the three loops, their per-item commits and the success log are registered findings, not yours to change.

- [ ] **Step 4: Confirm they pass, then run the WHOLE suite**

```bash
./run_tests.sh -q
```

Foreground, once. Report any failure other than your own tests rather than amending it.

- [ ] **Step 5: Commit the fix on its own**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_lock_delete.py
git commit -m "fix(activitypub): refuse Delete's unknown feed and sender before dereferencing them

The ownership check read feed.user_id and user.id before either had been
checked for None, so a Delete naming a feed this instance does not have
raised AttributeError -- and the else branch written to log exactly that
case was unreachable behind it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: The rest of the Delete arm

**Files:**
- Modify: `tests/test_inbox_dispatch_lock_delete.py`

`routes.py:1264-1327`.

- [ ] **Step 1: Cover the feed-deletion success path**

Seed a feed owned by the sender with two `FeedItem`s, two `FeedMember`s and one `FeedJoinRequest`, then assert all are gone, the feed itself is gone, and the success message names the feed's id. Use `make_feed_item` and `make_feed_member` from Task 1.

- [ ] **Step 2: Cover the non-owner refusal**

`:1273-1275`, `'Delete rejected, request came from non-owner.'` — a feed that exists but belongs to someone else.

- [ ] **Step 3: Cover the three object shapes**

`:1266` dict-with-`type`-`Feed`; `:1302` a bare string (Lemmy); `:1304` a dict with `id` (kbin). Three tests establishing that each reaches `find_liked_object` with the right `ap_id` — except the Feed shape, which returns earlier.

- [ ] **Step 4: Cover the content-deletion paths**

`:1306-1317`: content already deleted (`APLOG_IGNORED`, `'Activity about local content which is already deleted'`); content deleted successfully (`delete_post_or_comment` called with the reason from `summary`, and `announce_activity_to_followers` called only when not announced). Assert the delegate arguments.

- [ ] **Step 5: Cover the PM path and its silence**

`:1319-1326`: a `ChatMessage` matching `ap_id` and `sender_id` is marked read and deleted with a SUCCESS log; **no match logs nothing at all**. Assert the silent case with `ActivityPubLog.query.count() == 0` and `LOG_ACTIVITYPUB_TO_DB` enabled. Register it, do not fix it.

- [ ] **Step 6: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_lock_delete.py -q && \
  git add tests/test_inbox_dispatch_lock_delete.py && \
  git commit -m "test: cover the Delete arm's feed, content and PM paths

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: FIX 4 — Add's auto-subscribe loop runs without a community

**Files:**
- Create: `tests/test_inbox_dispatch_add_remove.py`
- Modify: `app/activitypub/routes.py:1404-1419` **(authorised fix)**

**Interfaces:**
- Consumes: `record_moderation` from `tests/test_inbox_dispatch_lock_delete.py`, `make_feed_item`/`make_feed_member` from `tests/factories.py`.

- [ ] **Step 1: Write the failing test**

```python
def test_an_add_whose_community_cannot_be_resolved_does_not_touch_feed_members(
        app, db_session, monkeypatch):
    """routes.py:1404-1419. The FeedItem creation is guarded by
    `if community_to_add and isinstance(community_to_add, Community)`, but the
    feed_members loop below it is NOT -- and :1418 reads
    community_to_add.ap_id, so an unresolvable community raises AttributeError
    once the feed has at least one member.

    Asserts the corrected behaviour: no FeedItem, no do_subscribe call, no
    crash.
    """
```

Seed a feed with one local member who has `feed_auto_follow` True, and an `Add` whose `object.id` does not resolve. Double `do_subscribe`.

- [ ] **Step 2: Run it and record the failure**

Expected: FAIL with `AttributeError: 'NoneType' object has no attribute 'ap_id'`. Record the exact text.

- [ ] **Step 3: Apply the fix**

Move the `feed_members` loop inside the guard that establishes what it operates on — indent `:1409-1419` (the comment, the query, and the `for` body) one level, so it sits under `if community_to_add and isinstance(community_to_add, Community):`.

Change nothing else. The inline `from app.community.routes import do_subscribe` at `:1417` moves with the block; it is a registered finding, not yours to fix.

- [ ] **Step 4: Confirm it passes, then run the WHOLE suite**

```bash
./run_tests.sh -q
```

Foreground, once. Report any other failure rather than amending it.

- [ ] **Step 5: Commit the fix on its own**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_add_remove.py
git commit -m "fix(activitypub): only auto-subscribe feed members to a community that was added

The loop exists to subscribe a feed's members to the community just added,
but sat outside the guard establishing that a community was resolved at all,
and dereferenced it. An Add naming an unresolvable community raised
AttributeError for any feed with members.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: The rest of the Add arm

**Files:**
- Modify: `tests/test_inbox_dispatch_add_remove.py`

`routes.py:1396-1468`.

- [ ] **Step 1: Derive the arm's outcome table from source** and write it into the file as a comment, noting every path that logs nothing.

- [ ] **Step 2: Cover the feed branch's success path, and pin its silence**

A resolvable community is added: a `FeedItem` exists, `feed.num_communities` incremented from a seeded value, and each local member with `feed_auto_follow` gets a `do_subscribe` call — except the feed's owner, whom `:1413-1414` skips. Assert the owner is skipped and a non-owner is not.

Then pin that **the whole feed branch logs nothing** on any outcome: `ActivityPubLog.query.count() == 0` with logging enabled. Register it, do not fix it.

- [ ] **Step 3: Cover the permission guard**

`:1421-1423`, `'Does not have permission'`. Drop each half of
`not community.is_moderator(mod) and not community.is_instance_admin(mod)`
separately; each needs a distinct killer — a moderator who is not an instance admin, and an instance admin who is not a moderator.

- [ ] **Step 4: Cover the featured-URL (sticky) target**

`:1429-1437`: `post.sticky` True and SUCCESS; post not found and `'Cannot find: '`. Note `:1425-1426` backfills `community.ap_featured_url` when empty — cover both the backfill and the pre-set case, and assert the comparison is case-insensitive (`:1429` lowercases both sides).

- [ ] **Step 5: Cover the moderators-URL target**

`:1439-1462`: a new moderator with no existing membership (a `CommunityMember` with `is_moderator=True` is created); one with an existing membership (the row is flipped rather than duplicated); and an unresolvable actor (`'Cannot find: '`). Assert `add_to_modlog('add_mod', ...)` arguments.

- [ ] **Step 6: Cover the two remaining outcomes**

`:1464`, `'Unknown target for Add'` — a target matching neither URL. `:1466-1467`, `'Add: cannot find community or feed'` — neither `community` nor `feed` set.

- [ ] **Step 7: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_add_remove.py -q && \
  git add tests/test_inbox_dispatch_add_remove.py && \
  git commit -m "test: cover the Add arm's feed, sticky and moderator targets

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: FIX 5 and FIX 6 — Remove's unguarded feed item and membership

**Files:**
- Modify: `tests/test_inbox_dispatch_add_remove.py`
- Modify: `app/activitypub/routes.py:1478-1494` **(authorised fix)**

- [ ] **Step 1: Write two failing tests**

```python
def test_a_remove_for_a_community_not_in_the_feed_is_a_no_op(app, db_session, monkeypatch):
    """routes.py:1478-1481. The FeedItem lookup returns None when the
    community was never in the feed, and session.delete(None) raises --
    and num_communities would be decremented for a removal that never
    happened.

    Asserts the corrected behaviour: no crash, num_communities unchanged.
    """


def test_a_remove_skips_a_feed_member_with_no_community_membership(
        app, db_session, monkeypatch):
    """routes.py:1492-1494. cm is None when the member never joined the
    community being removed, and `cm.joined_via_feed` raises.

    Asserts the corrected behaviour: that member is skipped, no crash.
    """
```

The second needs a feed member who is local, has `feed_auto_leave` True, and has **no** `CommunityMember` row for the community being removed.

- [ ] **Step 2: Run them and record both failures**

Expected: `session.delete(None)` raising, and `AttributeError: 'NoneType' object has no attribute 'joined_via_feed'`. Record both exactly.

- [ ] **Step 3: Apply both fixes**

```python
# :1478-1481 — guard the delete and the decrement together
                            feed_item = session.query(FeedItem).filter_by(feed_id=feed.id,
                                                                 community_id=community_to_remove.id).first()
                            if feed_item:
                                session.delete(feed_item)
                                feed.num_communities -= 1
                                session.commit()

# :1494 — short-circuit before the dereference
                                    if subscription != SUBSCRIPTION_OWNER and cm and cm.joined_via_feed:
```

Note the auto-unsubscribe loop below `:1481` currently sits inside the `if community_to_remove and isinstance(...)` guard and must **stay** there — do not move it under the new `if feed_item:`.

- [ ] **Step 4: Confirm they pass, then run the WHOLE suite**

```bash
./run_tests.sh -q
```

Foreground, once. Report any other failure rather than amending it.

- [ ] **Step 5: Commit the fix on its own**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_add_remove.py
git commit -m "fix(activitypub): guard Remove's feed item and community membership lookups

session.delete(None) raised when the community was never in the feed, and
the decrement would have run for a removal that did not happen; and the
auto-unsubscribe loop read cm.joined_via_feed for members who had no
membership of the community being removed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Remove's feed branch and its auto-unsubscribe loop

**Files:**
- Modify: `tests/test_inbox_dispatch_add_remove.py`

`routes.py:1474-1522`, the richest single block in this sub-project.

- [ ] **Step 1: Cover the removal itself**

A community that IS in the feed: the `FeedItem` is gone, `num_communities` decremented from a seeded value.

- [ ] **Step 2: Cover the loop's three skip conditions separately**

`:1488-1494` skips the feed owner (`fm_user.id == feed.user_id`), non-local users, users with `feed_auto_leave` False, and — post-Fix 6 — users with no `CommunityMember`. Each needs its own test with only that condition true, so no two are satisfied by one seeded row.

- [ ] **Step 3: Cover the local-community path**

A local `community_to_remove` skips the whole `if not community_to_remove.is_local():` block at `:1497` and goes straight to `if proceed:`. Assert no send was recorded.

- [ ] **Step 4: Cover the remote path and assert the Undo payload**

`:1498-1513`: for a remote community whose instance is not `gone_forever`, an `Undo` wrapping a `Follow` is sent. Use `record_sends` from `tests/test_inbox_dispatch_follow.py` and assert the body's `type` is `'Undo'`, its `object.type` is `'Follow'`, its `actor` is the member's `public_url()`, its `object.object` is the community's `public_url()`, and the `key_id` is `fm_user.public_url() + '#main-key'`. A send-count-only assertion leaves the federation contract untested.

Cover `gone_forever` True separately: no send, but the membership is still deleted.

- [ ] **Step 5: Cover the ovo.st special case**

`:1500-1505` replaces the generated `follow_id` with one built from the join request's `uuid` when the community's instance domain is `ovo.st`. Two tests: that domain with a join request present, and the same domain with none (the generated id is kept). Register the hardcoded domain as a finding; do not fix it.

- [ ] **Step 6: Cover the deletion and its log**

`:1517-1521`: the `CommunityMember` and `CommunityJoinRequest` rows are deleted, `subscriptions_count` decremented from a seeded value, and SUCCESS logged with the member's name and the community's `ap_public_url`.

- [ ] **Step 7: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_add_remove.py -q && \
  git add tests/test_inbox_dispatch_add_remove.py && \
  git commit -m "test: cover Remove's feed branch and its auto-unsubscribe loop

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Remove's community branch

**Files:**
- Modify: `tests/test_inbox_dispatch_add_remove.py`

`routes.py:1523-1570`, the mirror of Task 7's Add coverage.

- [ ] **Step 1: Cover the permission guard, the unsticky target and the moderator-removal target**

`:1527-1529` refusal; `:1537-1545` `post.sticky` False and SUCCESS, or `'Cannot find: '`; `:1545-1565` an existing membership's `is_moderator` flipped False with SUCCESS, an unresolvable actor with `'Cannot find: '`, and `:1566` `'Unknown target for Remove'`, and `:1568` `'Remove: cannot find community or feed'`.

- [ ] **Step 2: Pin the four APLOG_ADD mislabellings**

`:1528`, `:1563`, `:1566` and `:1568` pass `APLOG_ADD`, while `:1524`, `:1540`, `:1542` and `:1559` correctly pass `APLOG_REMOVE`. Write one test asserting the stored `activity_type` for a refusal in this arm is what `APLOG_ADD` produces — documenting the defect, not endorsing it. Say so in the docstring. Same class as D63 and D68.

- [ ] **Step 3: Pin the modlog-without-membership path**

`:1560-1561`'s `add_to_modlog('remove_mod', ...)` sits **outside** the `if existing_membership:` block, so removing a moderator who has no membership row writes a modlog entry and logs nothing. Assert both halves: the modlog call happened, and `ActivityPubLog.query.count() == 0` with logging enabled. Register it, do not fix it.

- [ ] **Step 4: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_add_remove.py -q && \
  git add tests/test_inbox_dispatch_add_remove.py && \
  git commit -m "test: cover Remove's community branch and pin its APLOG_ADD mislabelling

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: The Block arm

**Files:**
- Create: `tests/test_inbox_dispatch_block.py`

`routes.py:1590-1671`. The one arm with no authorised fix.

- [ ] **Step 1: Derive the arm's outcome table from source**, noting every silent path.

- [ ] **Step 2: Cover the pre-target checks**

`:1607-1608` (`cc` emptied when not announced and storing JSON); `:1614-1615` blocked user unknown here (`APLOG_IGNORED`, `'Does not exist here'`); `:1617-1618` already banned.

- [ ] **Step 3: Cover the site-ban path**

`target.count('/') < 4` at `:1623`. Non-admin refused (`:1624-1626`); a local blocked user (`ban_user` called, `APLOG_MONITOR`, `'Remote Admin in banning one of our users from their site'`); a blocked user on a third instance (`APLOG_MONITOR`, different message); and the ordinary case where `blocked.banned` is set, `ban_until` taken from `expires` or `endTime`, and `site_ban_remove_data` called only when `removeData` is set.

- [ ] **Step 4: Establish what `ban_until` actually does with a peer-supplied string**

`:1640-1642` assigns `core_activity['expires']` — a raw string — to a `DateTime` column. Write a test that establishes the observed behaviour rather than assuming SQLAlchemy coerces it: does the commit succeed, and what does the column read back as? Assert what you observe and record it for the register.

- [ ] **Step 5: Cover the community-ban path**

`:1649-1663`: community resolved from `target` when not already set; unfound community (`APLOG_IGNORED`, `'Blocked or unfound community'`); the permission guard at `:1655` with each half dropped separately and distinct killers; `community_ban_remove_data` called only when `removeData`; `ban_user` called only when not already banned; SUCCESS logged either way.

- [ ] **Step 6: Cover the Mastodon no-target path and its silence**

`:1664-1668`: a string object with no `target` creates a `UserBlock` when none exists — and logs nothing on any outcome, including when `object` is not a string. Assert with `ActivityPubLog.query.count() == 0` and logging enabled. Register it, do not fix it.

- [ ] **Step 7: Probe the dict-shaped object**

`:1612` reads `core_activity['object'].lower()` before the `isinstance` check the Mastodon path applies at `:1665`. Establish and assert what a dict-shaped `object` actually does. Do not fix it.

- [ ] **Step 8: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_block.py -q && \
  git add tests/test_inbox_dispatch_block.py && \
  git commit -m "test: cover the Block arm's site ban, community ban and Mastodon paths

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Whole-unit confirmation and the register

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**Report only. Fix nothing.**

- [ ] **Step 1: Measure the five spans whole**

```bash
./run_tests.sh tests/test_inbox_dispatch_lock_delete.py tests/test_inbox_dispatch_add_remove.py tests/test_inbox_dispatch_block.py -q --cov=app.activitypub.routes --cov-report=json
```

Foreground, once. Read `executed_lines` and `missing_branches` against `1264-1327`, `1356-1395`, `1396-1468`, `1469-1570` and `1590-1671`. Note that Tasks 4, 6 and 8 changed line counts in three of those spans — re-derive the spans' boundaries from source rather than trusting these numbers, and report the correction if they moved.

Report the figures per span and **explain every gap**. An unexplained remainder fails this task.

- [ ] **Step 2: File the findings**

The register's next free number is **D80** — verify that before relying on it, then take numbers in order and update the allocation ledger in the same commit.

At minimum: the four `APLOG_ADD` sites in `Remove`; `Add`'s silent feed branch; `Block`'s silent Mastodon path; `add_to_modlog('remove_mod')` running without a membership; `Delete`'s per-item commits in three loops; `Delete`'s silent PM-not-found path; `core_activity['object'].lower()` on a dict; the unguarded `target` reads in `Add` and `Remove`; the hardcoded `ovo.st` domain; the inline import at `:1417`; and whatever Task 11 Step 4 established about `ban_until`.

Argue each severity from **what the tests executed**, not from reading, and mark reading-level claims as such.

- [ ] **Step 3: Record the six fixes as fixed**

Each gets a row marked fixed with its commit SHA, and the section states that this sub-project was authorised to fix exactly these six while registering everything else — a deliberate, bounded departure from the campaign's rule, larger than 5b's two.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -m "docs: register the moderation arms' findings

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: The floor, and the harness notes

**Files:**
- Modify: `coverage_floors.ini`, `tests/README.md`

- [ ] **Step 1: Measure the full suite**

```bash
./run_tests.sh -q --cov=app.activitypub.routes --cov-report=json
```

Foreground, once. The whole suite, not this sub-project's files.

- [ ] **Step 2: Raise the floor**

Set `app/activitypub/routes.py` to one point below the measured blended figure, as 5b set 35 against 35.735 and 5a set 26 against 26.378. Record the measured figure in the commit message. **Floors only rise** — if the measurement is at or below 35, stop and report BLOCKED with the figures.

- [ ] **Step 3: Add this slice's harness facts to `tests/README.md`**

Append to the existing inbox-dispatch harness section: `make_feed_item` and `make_feed_member`; that `record_moderation` doubles moderation delegates at their routes-module binding site and lives in `tests/test_inbox_dispatch_lock_delete.py`; and that `Delete` and `Lock` resolve their own targets from `core_activity['object']` while `Add`, `Remove` and `Block` take `community`/`feed` from the preamble — the distinction a test needs in order to choose a branch.

- [ ] **Step 4: Run the ratchet and commit**

```bash
./run_tests.sh -q && \
  git add coverage_floors.ini tests/README.md && \
  git commit -m "docs: raise app/activitypub/routes.py's floor after the moderation arms

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Plan self-review

**Spec coverage.** All five spans map to tasks: `Lock` to Tasks 2–3, `Delete` to Tasks 4–5, `Add` to Tasks 6–7, `Remove` to Tasks 8–10, `Block` to Task 11. The spec's six authorised fixes are Tasks 2 (Fix 1), 4 (Fixes 2 and 3), 6 (Fix 4) and 8 (Fixes 5 and 6) — each test-first, each committed alone, each followed by a full-suite run. The spec's ordering requirement holds: no arm's coverage is written before its fix.

**Every item in the spec's "what reading has already found" has a task that pins it** — the `APLOG_ADD` sites at Task 10 Step 2, `Add`'s silent feed branch at Task 7 Step 2, `Block`'s silent Mastodon path at Task 11 Step 6, `add_to_modlog` without membership at Task 10 Step 3, `Delete`'s per-item commits and silent PM path at Task 5, the dict-object probe at Task 11 Step 7, `ovo.st` at Task 9 Step 5, and the rest at Task 12's register.

**The spec's three named risks are addressed:** the full-suite run after every fix (each fix task's Step 4); `Lock`'s restored subtree update asserted rather than assumed (Task 2 Step 1 requires a child reply and the subtree assertion); and `send_post_request` doubled with its payload asserted (Task 9 Step 4).

**Interface consistency.** `make_feed_item(feed, community)`, `make_feed_member(user, feed, is_owner=False)` and `record_moderation(monkeypatch, *names)` are defined once in Task 1 with the signatures Tasks 3–11 use. `dispatch` and `record_sends` keep the signatures 5a and 5b gave them.

**Line numbers will move.** Tasks 4, 6 and 8 change the length of three spans. Task 12 Step 1 says so explicitly and requires re-derivation from source rather than trusting the spec's figures — the correction 5b's Task 9 had to make about the Reject span, anticipated this time.

**One thing may prove awkward** and is handled by reporting rather than contrivance: Task 11 Step 4's `ban_until` probe cannot know in advance whether the raw string is coerced, rejected, or stored as-is, so it establishes observed behaviour and registers it.

# Sub-project 12: moderation and ban-removal coverage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `delete_post_or_comment`, `restore_post_or_comment`, `site_ban_remove_data`, `community_ban_remove_data`, `ban_user` and `unban_user` to full statement coverage, fix the write to a non-existent column, and register the rest.

**Architecture:** One new test file calling the six functions **directly** with real rows, asserting on persisted database state. Four existing suites already drive these through the inbox dispatcher and largely double them, so direct calls complement rather than duplicate.

**Tech Stack:** pytest, SQLAlchemy, `tests/factories.py`, a lock-only fakeredis double.

**Spec:** `docs/superpowers/specs/2026-09-03-coverage-moderation-12-design.md`

## Global Constraints

- Defects found **inside these six functions** are fixed test-first, each in its own commit, each proved by a mutation that fails a named test. Anything outside them is **registered, not fixed**.
- Findings are numbered from **D200**, and **both live "Next free number" notes** are updated in the same change. The historical notes are frozen records — leave them alone.
- The floor for `app/activitypub/util.py` rises to the measured blended figure rounded down. It currently reads **45**; measured is **48.9424%**.
- **Locate every code target by content, not by the line numbers in this plan.** Task 10's fix will shift lines.
- **Every test asserts on persisted database state**, not only on a return value — these functions return nothing.
- **No vacuous assertions.** Every counter defaults to 0, so a test asserting a decrement must seed a non-zero baseline or it cannot tell a working decrement from an absent one.
- The full suite must pass. **Only the controller runs it, one session at a time**, and the controller supplies every coverage figure. Do not run a coverage run.
- **Delete nothing** the task did not create. `claude_test` in the repository root is not the campaign's.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_ap_moderation.py` | **Create.** All tests for the six functions. |
| `tests/factories.py` | **Modify.** Add `make_community_ban`. |
| `app/activitypub/util.py` | **Modify, Task 10 only.** The `blocked.reply_count` fix. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Task 11 only.** |
| `tests/README.md` | **Modify, Task 11 only.** |
| `coverage_floors.ini` | **Modify, Task 11 only.** |

## Helpers each task inherits

SDD implementers see only their own brief, so this table is repeated into every brief. Task 1 creates these; Tasks 2-9 import and use them and **must not reimplement them**.

| Name | Where | Signature / behaviour |
|---|---|---|
| `seed_moderation_scene()` | this file | returns `(site, instance, community, author, moderator)`; author is the content owner, moderator is a `CommunityMember(is_moderator=True)` |
| `_double_file_deletion(monkeypatch)` | this file | patches `File.delete_from_disk`; returns a list of `(file_id, purge_cdn)` tuples |
| `make_community_ban(user, community, banned_by=None, reason='', ban_until=None)` | `tests/factories.py` | Task 1 adds it; composite PK, no `id` column |
| `make_user(instance, name, local=False, with_keys=False)` | `tests/factories.py` | leaves `ap_profile_id`/`ap_public_url`/`ap_id` `None` when `local=True` |
| `make_community(name='microblogs', host='test.piefed.local')` | `tests/factories.py` | |
| `make_post(community, user, ap_id, title='a post', private=False, microblog=False)` | `tests/factories.py` | **`ap_id` positional and required**; sets `deleted=False`, `status` at `POST_STATUS_PUBLISHED` |
| `make_post_reply(post, user, body='a reply')` | `tests/factories.py` | sets `deleted=False` explicitly; leaves `path` `None` |
| `make_community_member(user, community, is_moderator=False)` | `tests/factories.py` | |
| `make_instance_ban(user, instance)` | `tests/factories.py` | |
| `make_community_join_request(user, community, ...)` | `tests/factories.py` | |
| `make_notification_subscription(user, entity_id, type_, ...)` | `tests/factories.py` | |
| `redis_lock_only_double` | this file (Task 1) | **required by every `delete_post_or_comment` test** — `redis_double` does NOT work, see fact 2 |

## Facts every task needs

1. **`log_incoming_ap` writes NOTHING under test unless you enable it.** It is gated on `current_app.config['LOG_ACTIVITYPUB_TO_DB']`, which `config.py:92` defaults to `False` and neither `tests/conftest.py` nor `.env.test` overrides. So an `ActivityPubLog` assertion is **vacuous by default** — it will find zero rows whether the code logged or not. **Assert the real effect instead** — the row's `deleted` flag, the counter, the `ModLog` entry. Every function in this slice has one, so no test in this plan needs the log row. If you find a branch whose ONLY observable is the log, say so in your report rather than enabling the config on your own: it would be the first, and it changes what the suite proves.
2. **`delete_post_or_comment` takes SEVEN redis locks, and `redis_double` cannot serve them.** It does `from app import redis_client` **inside its body** — a re-executed import — so a patch of `app.redis_client` reaches it, and without one the test talks to the real, shared, never-truncated compose Redis. But the `redis_double` fixture is **not** the right patch: fakeredis with no `lupa` implements no Lua scripting, so `Lock.acquire()` succeeds (plain `SET NX PX`) and `Lock.release()` raises `unknown command 'evalsha'` (`tests/conftest.py:430-434` documents this). Task 1 defines a local `redis_lock_only_double` fixture, shaped after the existing one in `tests/test_inbox_dispatch_votes.py:145-158`. **Use `redis_lock_only_double`, never `redis_double`.** Only `delete_post_or_comment` locks; the other five functions take none.
3. **`File.delete_from_disk(purge_cdn=True)`** (`app/models.py`) touches the filesystem and a CDN. Both ban-removal functions call it in a loop. It must be doubled, and `purge_cdn`'s value must be observable — the site/community difference is a registered finding.
4. **`add_to_modlog` raises on an unknown action** (`app/utils.py`): `if action not in ModLog.action_map.keys(): raise Exception(...)`. The actions these six use are `delete_post`, `delete_post_reply`, `restore_post`, `restore_post_reply`, `ban_user`, `unban_user`.
5. **`CommunityBan` has a composite primary key** — `user_id` and `community_id`, no `id` column (`app/models.py`). Columns: `banned_by`, `reason`, `created_at`, `ban_until`.
6. **`Community.is_moderator(user)`, `Community.is_instance_admin(user)` and `Community.has_poster(user)`** all take the user as an argument. `User.is_instance_admin()` takes none.
7. **Every counter defaults to 0.** `Community.post_count`, `Community.post_reply_count`, `User.post_count`, `User.post_reply_count`, `Post.reply_count`, `Post.reply_count_cross_posted`. A decrement test must seed a non-zero baseline.
8. **`make_post_reply` leaves `path` as `None`**, so the `if to_delete.path:` child-count branch is not reached unless a test sets it.
9. The four existing suites — `tests/test_inbox_dispatch_lock_delete.py`, `test_inbox_dispatch_undo_moderation.py`, `test_inbox_dispatch_block.py`, `test_activitypub_ban_expiry.py` — assert the **dispatcher calls** these functions, mostly with the functions doubled. This slice asserts what the functions **do**. Read them if a fixture question arises, but do not repeat their shape.

---

### Task 1: Scaffold, helpers, the `CommunityBan` factory, and `delete_post_or_comment`'s Post happy path

**Files:**
- Create: `tests/test_ap_moderation.py`
- Modify: `tests/factories.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `seed_moderation_scene`, `_double_file_deletion`, and `make_community_ban`. Every later task uses these.

- [ ] **Step 1: Write the file header and helpers**

```python
"""tests/test_ap_moderation.py"""
from app import db
from app.activitypub import util as ap_util
from app.constants import NOTIF_REPORT, POST_STATUS_PUBLISHED
from app.models import File, ModLog, Notification, Post, PostReply, User
from tests.factories import (make_community, make_community_ban, make_community_member,
                             make_instance, make_post, make_post_reply, make_site, make_user,
                             seed_community_owner)


def seed_moderation_scene(community_name='books'):
    """A community with an owner, a content author and a separate moderator.

    `seed_community_owner` creates BOTH the Instance (id 1) and a local User
    (id 1), which `make_community` hardcodes against as real foreign keys.
    The moderator is a distinct user from the author so the
    `to_delete.author.id != deletor.id` modlog branch is reachable -- when
    the deletor IS the author, no modlog entry is written at all.
    """
    site = make_site()
    instance = seed_community_owner('peer.example')
    community = make_community(name=community_name, host='test.piefed.local')
    author = make_user(instance, 'contentauthor', local=True)
    moderator = make_user(instance, 'themoderator', local=True)
    make_community_member(moderator, community, is_moderator=True)
    db.session.commit()
    return site, instance, community, author, moderator


def _double_file_deletion(monkeypatch):
    """Stop File.delete_from_disk from touching the filesystem or the CDN.

    Both ban-removal functions call it in a loop over every File attached to
    the banned user's posts. It is patched on the MODEL CLASS rather than on
    a module, because both call sites reach it as a bound method on a row
    they just queried, not through an imported name.

    The recorded `purge_cdn` value is the observable for a registered
    asymmetry: `site_ban_remove_data` passes `purge_cdn=True` explicitly and
    `community_ban_remove_data` takes the default. Recording the flag is what
    lets a test state that difference rather than assert it from the source.
    """
    calls = []

    def fake_delete(self, purge_cdn=True):
        calls.append((self.id, purge_cdn))

    monkeypatch.setattr(File, 'delete_from_disk', fake_delete)
    return calls


```

- [ ] **Step 2: Add `make_community_ban` to `tests/factories.py`**

```python
def make_community_ban(user: User, community: Community, banned_by: User = None,
                       reason: str = '', ban_until=None) -> CommunityBan:
    """The row `ban_user` checks for before creating its own, and the row
    `unban_user` deletes.

    `CommunityBan` has a COMPOSITE primary key -- `user_id` and
    `community_id` together (app/models.py) -- and no `id` column, so a test
    that wants to assert the row is gone queries by both keys rather than by
    an id it never received.

    `banned_by` defaults to the community's own owner rather than to None,
    because a NULL `banned_by` is not a state `ban_user` can produce and a
    fixture that manufactures one would test a shape production never sees.
    """
    ban = CommunityBan(
        user_id=user.id,
        community_id=community.id,
        banned_by=banned_by.id if banned_by else community.user_id,
        reason=reason,
        ban_until=ban_until,
    )
    db.session.add(ban)
    db.session.commit()
    return ban
```

Add `CommunityBan` to the `from app.models import ...` block at the top of `tests/factories.py` if it is not already there.

- [ ] **Step 3: Write the Post deletion happy path**

```python
def test_a_moderator_deleting_a_post_marks_it_deleted_and_decrements_counters(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`delete_post_or_comment`'s Post branch, driven directly rather than
    through the inbox dispatcher.

    `redis_lock_only_double` is REQUIRED, not optional: this function takes
    `redis_client.lock(...)` on three keys in the Post branch and four more in
    the PostReply branch, and it does `from app import
    redis_client` inside its own body, so the fixture's patch of
    `app.redis_client` reaches it. Without the fixture the test talks to the
    real, shared, never-truncated Redis in the compose stack.

    Both counters are seeded to 5 before the call. They default to 0, and a
    decrement from 0 to -1 is indistinguishable from a decrement that did not
    happen if the assertion only checks "not 5" -- so the baseline is
    explicit and the assertion is an exact equality.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    community.post_count = 5
    author.post_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'},
                                   'spam')

    assert post.deleted is True
    assert post.deleted_by == moderator.id
    assert community.post_count == 4
    assert author.post_count == 4
```

- [ ] **Step 4: Write the modlog assertion**

```python
def test_deleting_another_users_post_writes_a_modlog_entry(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`if to_delete.author.id != deletor.id:` -- the modlog entry is written
    only when someone deletes SOMEONE ELSE'S content. A self-delete is not
    moderation and is not logged, which its twin below asserts.

    `add_to_modlog` runs for real here. It raises on an unknown action
    (app/utils.py), so the action string `delete_post` is itself under test:
    a typo would raise rather than silently write nothing.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'},
                                   'spam')

    entries = db.session.query(ModLog).filter_by(target_user_id=author.id).all()
    assert len(entries) == 1
    assert entries[0].action == 'delete_post'
    assert entries[0].reason == 'spam'


def test_an_author_deleting_their_own_post_writes_no_modlog_entry(
        app, db_session, monkeypatch, redis_lock_only_double):
    """The false side of `if to_delete.author.id != deletor.id:`. The post is
    still deleted -- both assertions are made, so this cannot pass by the
    deletion having been refused.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(author, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'},
                                   'changed my mind')

    assert post.deleted is True
    assert db.session.query(ModLog).count() == 0
```

**If `ModLog` has no `target_user_id` column**, read the model and use the real column name — do not weaken the assertion to a bare count.

- [ ] **Step 5: Run the tests**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_ap_moderation.py tests/factories.py
git commit -m "test: scaffold the moderation suite and cover post deletion"
```

---

### Task 2: `delete_post_or_comment`'s four-disjunct authorisation guard

**Files:**
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

The guard, located by content:

```python
if (to_delete.user_id == deletor.id or
        (deletor.instance_id == to_delete.author.instance_id and deletor.is_instance_admin()) or
        community.is_moderator(deletor) or
        community.is_instance_admin(deletor)):
```

**Four disjuncts, one of them itself a conjunction.** This is the largest guard in the campaign so far. Each needs its own test with the other three arranged to be false, or the mutations cannot be attributed.

- [ ] **Step 1: Write the refusal test first**

```python
def test_an_unrelated_user_cannot_delete_a_post(app, db_session, monkeypatch, redis_lock_only_double):
    """All four disjuncts false. This is the test every mutation below must
    leave passing -- if dropping a disjunct made an UNAUTHORISED delete
    succeed, this test is what catches it.

    The observable is that the post is NOT deleted. The failure branch calls
    `log_incoming_ap(..., APLOG_FAILURE, ..., 'Deletor did not have
    permisson')`, but that writes nothing unless LOG_ACTIVITYPUB_TO_DB is on,
    so the real effect is asserted instead.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    stranger = make_user(instance, 'stranger', local=True)
    post = make_post(community, author, None, title='a post')
    community.post_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(stranger, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'},
                                   'spam')

    assert post.deleted is False
    assert community.post_count == 5
```

- [ ] **Step 2: Write one test per disjunct**

```python
def test_the_author_may_delete_their_own_post(app, db_session, monkeypatch, redis_lock_only_double):
    """Disjunct 1: `to_delete.user_id == deletor.id`. The author is given NO
    moderator row and is not an instance admin, so the other three disjuncts
    are false and dropping this one fails exactly this test.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(author, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert post.deleted is True


def test_a_same_instance_admin_may_delete_a_post(app, db_session, monkeypatch, redis_lock_only_double):
    """Disjunct 2, which is itself a CONJUNCTION:
    `deletor.instance_id == to_delete.author.instance_id and
    deletor.is_instance_admin()`. Both halves must hold, and the two halves
    are mutation-tested separately in Step 4.

    The admin is NOT a community moderator, so disjunct 3 is false.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    admin = make_user(instance, 'theadmin', local=True)
    admin.instance_id = author.instance_id
    admin.roles.append(db.session.query(Role).filter_by(name='Admin').first())
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(admin, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert post.deleted is True


def test_a_community_moderator_may_delete_a_post(app, db_session, monkeypatch, redis_lock_only_double):
    """Disjunct 3: `community.is_moderator(deletor)`. `seed_moderation_scene`
    gives the moderator a CommunityMember row with `is_moderator=True` and
    nothing else -- not the author, not an admin -- so this disjunct is the
    only true one.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert post.deleted is True
```

**Disjunct 4, `community.is_instance_admin(deletor)`:** read `Community.is_instance_admin` in `app/models.py` and work out what makes it true for a user who is **not** already covered by disjunct 2. If it is reachable, write a fourth test in the same shape. **If it is NOT independently reachable** — for instance if it implies disjunct 2's conjunction — say so in your report with the reasoning, and record it as an unkillable clause rather than inventing a fixture. An unkillable clause is a finding; the campaign has catalogued three causes and "the clause is subsumed by an earlier disjunct" would be a fourth.

**If `Role` is not importable or there is no `Admin` role row**, read how `tests/test_factories_permissions.py` grants admin and follow that pattern.

- [ ] **Step 3: Run the tests**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 9 passed. (Disjunct 4 proved independently reachable, and my mutation table was wrong about one inner conjunct, so this task added two tests beyond the original estimate.)

- [ ] **Step 4: Mutation-test each disjunct separately**

| Mutation | Must fail | Must still pass |
|---|---|---|
| remove `to_delete.user_id == deletor.id or` | `test_the_author_may_delete_their_own_post` | the others |
| remove the whole disjunct-2 conjunction | `test_a_same_instance_admin_may_delete_a_post` | the others |
| in disjunct 2, drop `deletor.instance_id == to_delete.author.instance_id and` | nothing — **report this** | — |
| in disjunct 2, drop `and deletor.is_instance_admin()` | nothing — **report this** | — |
| remove `community.is_moderator(deletor) or` | `test_a_community_moderator_may_delete_a_post` | the others |

The two inner-conjunct mutations **weaken** the guard, so they let more deletes through and cannot fail a test that expects a delete to succeed. What they *can* fail is `test_an_unrelated_user_cannot_delete_a_post` — but only if the stranger satisfies the surviving half. **Work out whether your stranger does**, and if not, say so: an inner conjunct that no test can kill is a finding, and the remedy is a fixture where the stranger is a same-instance non-admin (killing the `is_instance_admin` half) and another where they are a different-instance admin (killing the instance-match half). Add those two tests if the analysis says they are needed.

Restore after each mutation and verify `git diff app/activitypub/util.py` is empty before committing.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ap_moderation.py
git commit -m "test: isolate every disjunct of the deletion authorisation guard"
```

---

### Task 3: `delete_post_or_comment`'s PostReply branch

**Files:**
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

The reply branch maintains **five** counters where the post branch maintains two. Every one needs a seeded baseline.

- [ ] **Step 1: Write the reply happy path**

```python
def test_deleting_a_reply_decrements_every_counter_it_maintains(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`delete_post_or_comment`'s PostReply branch. It decrements FIVE
    counters where the Post branch decrements two:
    `post.reply_count` (only when the author is not a bot),
    `post.reply_count_cross_posted` (only when already non-zero),
    `author.post_reply_count`, and `community.post_reply_count`.

    All four are seeded to 5. They default to 0, and `reply_count_cross_posted`
    is additionally guarded by `if to_delete.post.reply_count_cross_posted:`
    -- at its default of 0 that guard is FALSE, so a test resting on the
    default would never reach the decrement and would pass against its
    removal.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    reply = make_post_reply(post, author)
    post.reply_count = 5
    post.reply_count_cross_posted = 5
    author.post_reply_count = 5
    community.post_reply_count = 5
    author.bot = False
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, reply, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert reply.deleted is True
    assert reply.deleted_by == moderator.id
    assert post.reply_count == 4
    assert post.reply_count_cross_posted == 4
    assert author.post_reply_count == 4
    assert community.post_reply_count == 4
```

- [ ] **Step 2: Write the bot branch**

```python
def test_deleting_a_bots_reply_leaves_the_posts_reply_count_alone(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`if not to_delete.author.bot:` guards BOTH `post.reply_count` and
    `post.reply_count_cross_posted`. A bot's reply is deleted and its author
    and community counters still fall, but the post's do not.

    `bot` is set to True explicitly; `make_user` leaves it at the column
    default, so a test resting on that default would be asserting the wrong
    side of the branch.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    reply = make_post_reply(post, author)
    post.reply_count = 5
    author.post_reply_count = 5
    community.post_reply_count = 5
    author.bot = True
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, reply, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    assert reply.deleted is True
    assert post.reply_count == 5
    assert author.post_reply_count == 4
    assert community.post_reply_count == 4
```

- [ ] **Step 3: Write the `path` child-count branch**

```python
def test_deleting_a_nested_reply_decrements_its_ancestors_child_count(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`if to_delete.path:` runs raw SQL decrementing `child_count` for every
    id in `path[:-1]` -- the reply's ancestors, excluding itself.

    `make_post_reply` leaves `path` as None, so this branch is unreachable
    unless a test sets it. Both replies get an explicit `path`, and the
    PARENT's child_count is seeded to 5 so the decrement is an exact
    assertion rather than a "less than before".
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    parent = make_post_reply(post, author, body='parent')
    child = make_post_reply(post, author, body='child')
    parent.path = [parent.id]
    child.path = [parent.id, child.id]
    parent.child_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, child, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    db.session.refresh(parent)
    assert child.deleted is True
    assert parent.child_count == 4
```

**If `PostReply` has no `child_count` column**, read the model and report what the raw SQL actually updates — do not guess.

- [ ] **Step 4: Write the notification-deletion branch**

```python
def test_deleting_a_post_removes_its_notifications_but_keeps_report_notifs(
        app, db_session, monkeypatch, redis_lock_only_double):
    """The Post branch deletes Notifications whose `targets->>'post_id'`
    matches, EXCEPT those of type NOTIF_REPORT or NOTIF_REPORT_ESCALATION --
    the `continue` inside the loop.

    Two notifications are seeded against the same post so the test
    discriminates: an ordinary one that must go and a report one that must
    stay. A single-notification fixture could not tell a working exemption
    from a loop that deleted nothing.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    ordinary = Notification(title='reply to your post', user_id=author.id,
                            author_id=moderator.id, notif_type=0,
                            targets={'post_id': post.id})
    report = Notification(title='post reported', user_id=moderator.id,
                          author_id=author.id, notif_type=NOTIF_REPORT,
                          targets={'post_id': post.id})
    db.session.add_all([ordinary, report])
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')

    surviving = db.session.query(Notification).all()
    assert len(surviving) == 1
    assert surviving[0].notif_type == NOTIF_REPORT
```

**If `Notification` requires columns this omits**, read the model and add them. If `notif_type=0` collides with a real constant, use a non-report constant from `app/constants.py` and name it.

- [ ] **Step 5: Run the tests**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 13 passed.

- [ ] **Step 6: Mutation-test the two guards**

Drop `if not to_delete.author.bot:` — `test_deleting_a_bots_reply_leaves_the_posts_reply_count_alone` must fail and the happy path must pass. Drop the `NOTIF_REPORT` half of the `continue` condition — the notification test must fail. Report both kills and their types. Restore and verify the diff is empty.

- [ ] **Step 7: Commit**

```bash
git add tests/test_ap_moderation.py
git commit -m "test: cover the reply branch's five counters and notification cleanup"
```

---

### Task 4: `restore_post_or_comment`

**Files:**
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need. **Task 5 depends on these tests existing but not on their names.**

`restore_post_or_comment` carries the **same four-disjunct guard** as `delete_post_or_comment`. Do not re-test all four — Task 2 proved the shape. Test the refusal and one success, and say in your report that the guard is textually identical, which is itself the finding that it is duplicated rather than shared.

- [ ] **Step 1: Write the Post restore happy path**

```python
def test_restoring_a_post_clears_deleted_and_restores_counters(
        app, db_session, monkeypatch, redis_lock_only_double):
    """`restore_post_or_comment`'s Post branch. Note it takes NO redis locks
    where `delete_post_or_comment` wraps every one of these same counter
    mutations in one -- a registered asymmetry, not something this test
    fixes. `redis_lock_only_double` is still requested so the test is safe if that
    changes.

    Counters are seeded to 4 and asserted at 5, the mirror of Task 1's
    deletion test.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    post.deleted = True
    post.deleted_by = moderator.id
    community.post_count = 4
    author.post_count = 4
    db.session.commit()

    ap_util.restore_post_or_comment(moderator, post, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, 'appeal')

    assert post.deleted is False
    assert post.deleted_by is None
    assert community.post_count == 5
    assert author.post_count == 5
```

- [ ] **Step 2: Write the reply restore**

```python
def test_restoring_a_reply_restores_only_the_counters_it_knows_about(
        app, db_session, monkeypatch, redis_lock_only_double):
    """PINS A DEFECT. `restore_post_or_comment`'s PostReply branch increments
    `post.reply_count` (when not a bot) and `author.post_reply_count` -- and
    NOTHING ELSE. `delete_post_or_comment` decrements four counters for the
    same row.

    So `community.post_reply_count` and `post.reply_count_cross_posted` are
    asserted UNCHANGED here, which is the defect: a restore does not undo
    what the delete did. Task 5 proves this end to end with a round trip;
    this test states it for the restore call in isolation.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    reply = make_post_reply(post, author)
    reply.deleted = True
    post.reply_count = 4
    post.reply_count_cross_posted = 4
    author.post_reply_count = 4
    community.post_reply_count = 4
    author.bot = False
    db.session.commit()

    ap_util.restore_post_or_comment(moderator, reply, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, '')

    assert reply.deleted is False
    assert post.reply_count == 5
    assert author.post_reply_count == 5
    assert community.post_reply_count == 4      # NOT restored -- the defect
    assert post.reply_count_cross_posted == 4   # NOT restored -- the defect
```

- [ ] **Step 3: Write the refusal**

```python
def test_an_unrelated_user_cannot_restore_a_post(app, db_session, monkeypatch, redis_lock_only_double):
    """The same four-disjunct guard `delete_post_or_comment` carries, copied
    rather than shared -- verified textually identical, which is why this
    task tests the refusal and one success instead of repeating Task 2's
    four-way isolation.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    stranger = make_user(instance, 'stranger', local=True)
    post = make_post(community, author, None, title='a post')
    post.deleted = True
    db.session.commit()

    ap_util.restore_post_or_comment(stranger, post, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, '')

    assert post.deleted is True
```

- [ ] **Step 4: Write the modlog assertion**

```python
def test_restoring_another_users_post_writes_a_restore_modlog_entry(
        app, db_session, monkeypatch, redis_lock_only_double):
    """Action string `restore_post`, distinct from deletion's `delete_post`.
    `add_to_modlog` raises on an unknown action, so the string is under test.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    post.deleted = True
    db.session.commit()

    ap_util.restore_post_or_comment(moderator, post, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, 'appeal')

    entries = db.session.query(ModLog).all()
    assert len(entries) == 1
    assert entries[0].action == 'restore_post'
```

- [ ] **Step 5: Run and commit**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 17 passed.

```bash
git add tests/test_ap_moderation.py
git commit -m "test: cover restore and pin the counters it fails to restore"
```

---

### Task 5: The delete/restore round trip

**Files:**
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

**This is the task the pairing exists for.** Task 4 asserted that `restore` leaves two counters alone. That is only a defect if `delete` moved them — otherwise it is correct behaviour. A round trip is the only shape that proves the drift.

- [ ] **Step 1: Write the round trip**

```python
def test_a_delete_then_restore_cycle_permanently_loses_two_counters(
        app, db_session, monkeypatch, redis_lock_only_double):
    """PINS A DEFECT, and it is this slice's most consequential.

    `delete_post_or_comment` decrements four counters for a reply;
    `restore_post_or_comment` increments two. So a delete followed by a
    restore -- the exact sequence a moderator produces by removing a comment
    and then reversing it on appeal -- leaves `community.post_reply_count`
    and `post.reply_count_cross_posted` permanently one lower. Every
    subsequent cycle loses one more, and nothing later notices or repairs it.

    A single-direction test cannot show this. Asserting that restore leaves a
    counter alone is only a defect if delete moved it, so the two calls have
    to happen in one test with the starting values recorded.

    DO NOT FIX -- registered, because correcting a counter changes numbers
    users already see and the historical drift is unknown.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    reply = make_post_reply(post, author)
    post.reply_count = 5
    post.reply_count_cross_posted = 5
    author.post_reply_count = 5
    community.post_reply_count = 5
    author.bot = False
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, reply, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')
    ap_util.restore_post_or_comment(moderator, reply, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, '')

    assert reply.deleted is False
    # The two counters both halves maintain come back:
    assert post.reply_count == 5
    assert author.post_reply_count == 5
    # The two only the delete side maintains do NOT:
    assert community.post_reply_count == 4
    assert post.reply_count_cross_posted == 4
```

- [ ] **Step 2: Write the Post-side round trip as a contrast**

```python
def test_a_post_delete_then_restore_cycle_is_lossless(
        app, db_session, monkeypatch, redis_lock_only_double):
    """The contrast that makes the reply finding sharp: the POST branches of
    both functions maintain the same two counters, so a post round trip
    returns to exactly where it started.

    Without this test the reply result reads as "these functions are sloppy
    about counters". With it, the finding is specific: the Post branches
    agree and the PostReply branches do not.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    community.post_count = 5
    author.post_count = 5
    db.session.commit()

    ap_util.delete_post_or_comment(moderator, post, False,
                                   {'id': 'https://peer.example/activities/delete/1'}, '')
    ap_util.restore_post_or_comment(moderator, post, False,
                                    {'id': 'https://peer.example/activities/undo/1'}, '')

    assert post.deleted is False
    assert community.post_count == 5
    assert author.post_count == 5
```

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 19 passed.

```bash
git add tests/test_ap_moderation.py
git commit -m "test: prove the delete/restore cycle loses two reply counters"
```

---

### Task 6: `site_ban_remove_data`, and pin the non-existent column

**Files:**
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: Task 1's helpers, including `_double_file_deletion`.
- Produces: `test_a_site_ban_does_not_zero_the_users_reply_count`, which **Task 10 inverts**. Task 10 needs that exact name.

**DO NOT FIX ANYTHING IN THIS TASK.** The column defect is pinned here and fixed in Task 10, in its own commit, so the fix has a witnessed pre-fix failure.

- [ ] **Step 1: Write the content-deletion happy path**

```python
def test_a_site_ban_deletes_every_post_and_reply_the_user_made(
        app, db_session, monkeypatch):
    """`site_ban_remove_data` filters on `user_id` alone -- no community
    filter, unlike `community_ban_remove_data`. So content in EVERY community
    goes, which is what a site ban means.

    Two communities are seeded precisely to prove the absence of that filter:
    a one-community fixture cannot tell "all the user's content" from "this
    community's content".
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    other = make_community(name='elsewhere', host='test.piefed.local')
    here = make_post(community, author, None, title='here')
    there = make_post(other, author, None, title='there')
    reply = make_post_reply(here, author)
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert here.deleted is True
    assert there.deleted is True
    assert reply.deleted is True
    assert here.deleted_by == moderator.id
```

- [ ] **Step 2: PIN the non-existent-column defect**

```python
def test_a_site_ban_does_not_zero_the_users_reply_count(app, db_session, monkeypatch):
    """PINS A DEFECT. DO NOT FIX -- a later task does.

    `site_ban_remove_data` contains `blocked.reply_count = 0`. `User` has no
    `reply_count` column: it declares `post_count` and `post_reply_count`
    (app/models.py), and `reply_count` belongs to `Post`. SQLAlchemy accepts
    the assignment as an ordinary Python attribute on the instance, so it
    never reaches the database and never raises.

    The effect is that a site-banned user's REAL reply counter keeps its
    pre-ban value forever, while `blocked.post_count = 0` on the very next
    line works because that column does exist. `community_ban_remove_data`
    decrements the real `post_reply_count` correctly.

    Both counters are seeded to 5 so the contrast is exact: post_count is
    asserted at 0 (the working line) and post_reply_count at 5 (the broken
    one). Asserting only the second would leave a reader unable to tell a
    bug from a deliberate choice not to zero anything.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    make_post_reply(post, author)
    author.post_count = 5
    author.post_reply_count = 5
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert author.post_count == 0        # the line that works
    assert author.post_reply_count == 5  # the line that does not -- the defect
```

- [ ] **Step 3: Write the file-deletion assertions**

```python
def test_a_site_ban_deletes_attached_files_and_purges_the_cdn(
        app, db_session, monkeypatch):
    """`site_ban_remove_data` calls `delete_from_disk(purge_cdn=True)`
    EXPLICITLY, where `community_ban_remove_data` takes the default. The
    recorded flag is asserted so the asymmetry is observable in the suite
    rather than read off the source.

    `source_url` is set to '' afterwards by the function; it is asserted too,
    because a File whose bytes are gone but whose source_url still points at
    them is a broken row rather than a deleted one.
    """
    calls = _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    attached = File(source_url='https://peer.example/img.png')
    db.session.add(attached)
    db.session.commit()
    post.image_id = attached.id
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert (attached.id, True) in calls
    assert attached.source_url == ''
```

**If `File` requires more columns**, read the model and add them. **If the `File`-to-`Post` join needs a different column than `image_id`**, read the query in `site_ban_remove_data` and follow it exactly — it joins `File` to `Post`, so whatever column that join uses is the one to set.

- [ ] **Step 4: Write the avatar/cover branch**

```python
def test_a_site_ban_deletes_the_users_avatar_and_cover(app, db_session, monkeypatch):
    """`if blocked.avatar_id:` and `if blocked.cover_id:` -- both guarded, and
    both are None on a factory-built user, so a test resting on the default
    would never reach either branch.

    `community_ban_remove_data` has NO equivalent: a community ban leaves the
    avatar alone. That asymmetry is defensible and the source says so; it is
    registered, not fixed.
    """
    calls = _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    avatar = File(source_url='https://peer.example/avatar.png')
    cover = File(source_url='https://peer.example/cover.png')
    db.session.add_all([avatar, cover])
    db.session.commit()
    author.avatar_id = avatar.id
    author.cover_id = cover.id
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert avatar.id in [c[0] for c in calls]
    assert cover.id in [c[0] for c in calls]
    assert avatar.source_url == ''
    assert cover.source_url == ''
```

- [ ] **Step 5: Write the already-deleted exclusion**

```python
def test_a_site_ban_skips_content_already_deleted(app, db_session, monkeypatch):
    """Both queries filter `deleted=False`. An already-deleted post must not
    have its counters decremented a second time, which is what that filter
    prevents.

    The community's counter is seeded and asserted to prove the row was
    skipped rather than merely left flagged: a post already deleted is
    already flagged, so `deleted is True` alone cannot discriminate.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    already = make_post(community, author, None, title='already gone')
    already.deleted = True
    community.post_count = 5
    db.session.commit()

    ap_util.site_ban_remove_data(moderator.id, author)

    assert community.post_count == 5
```

- [ ] **Step 6: Run the tests, then mutation-test the `deleted=False` filter**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 24 passed. (A controller-ordered fix round then added a sixth test — one seeding an already-deleted reply — to close a surviving mutant on the reply query's `deleted=False`, so the running total leaving this task is **25**.)

Drop `deleted=False` from the Post query — `test_a_site_ban_skips_content_already_deleted` must fail. Then drop it from the reply query instead; **that is a separate call site and needs its own kill.** If no test dies on the reply site, that is a combinatorial gap of the kind sub-project 11 hit — report it rather than patching around it, and the controller will rule. Restore and verify the diff is empty.

- [ ] **Step 7: Commit**

```bash
git add tests/test_ap_moderation.py
git commit -m "test: cover site ban data removal and pin its non-existent column write"
```

---

### Task 7: `community_ban_remove_data`

**Files:**
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

- [ ] **Step 1: Write the scoping test — the whole point of this function**

```python
def test_a_community_ban_deletes_only_that_communitys_content(
        app, db_session, monkeypatch):
    """`community_ban_remove_data` filters on `user_id` AND `community_id`,
    where `site_ban_remove_data` filters on `user_id` alone. Content the same
    user made elsewhere must survive.

    Two communities are required. With one, the community filter is
    unkillable -- dropping it changes nothing observable, exactly the
    combinatorial gap sub-project 11 hit on `post_ap_context`'s post_id.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    other = make_community(name='elsewhere', host='test.piefed.local')
    here = make_post(community, author, None, title='here')
    there = make_post(other, author, None, title='there')
    here_reply = make_post_reply(here, author)
    there_reply = make_post_reply(there, author)
    db.session.commit()

    ap_util.community_ban_remove_data(moderator.id, community.id, author)

    assert here.deleted is True
    assert here_reply.deleted is True
    assert there.deleted is False
    assert there_reply.deleted is False
```

- [ ] **Step 2: Write the counter test**

```python
def test_a_community_ban_decrements_the_users_real_reply_counter(
        app, db_session, monkeypatch):
    """The contrast with `site_ban_remove_data`'s broken line: this function
    does `blocked.post_reply_count -= 1` per reply, against the real column,
    and it works. Both are asserted so the pair reads as one finding.

    Counters seeded to 5; one post and one reply are removed, so both fall
    to 4 -- a relative decrement, unlike the site path's absolute zeroing.
    """
    _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    make_post_reply(post, author)
    author.post_count = 5
    author.post_reply_count = 5
    author.bot = False
    db.session.commit()

    ap_util.community_ban_remove_data(moderator.id, community.id, author)

    assert author.post_count == 4
    assert author.post_reply_count == 4
```

- [ ] **Step 3: Write the CDN asymmetry test**

```python
def test_a_community_ban_deletes_files_without_purging_the_cdn(
        app, db_session, monkeypatch):
    """PINS A NON-ASYMMETRY, which is why the assertion is `is True`.

    `community_ban_remove_data` calls `delete_from_disk()` with no argument
    and `site_ban_remove_data` passes `purge_cdn=True` explicitly -- but the
    parameter DEFAULTS to True (`app/models.py`), so both paths purge and the
    difference is in the spelling alone.

    Asserting the flag's real value is what makes that legible: reading the
    two call sites side by side invites the conclusion that a community ban
    leaves files on the CDN, and this test says otherwise in the suite rather
    than leaving the next reader to check the signature.
    """
    calls = _double_file_deletion(monkeypatch)
    site, instance, community, author, moderator = seed_moderation_scene()
    post = make_post(community, author, None, title='a post')
    attached = File(source_url='https://peer.example/img.png')
    db.session.add(attached)
    db.session.commit()
    post.image_id = attached.id
    db.session.commit()

    ap_util.community_ban_remove_data(moderator.id, community.id, author)

    assert len(calls) == 1
    assert calls[0][0] == attached.id
    assert calls[0][1] is True   # the default -- same as the site path passes
    assert attached.source_url == ''
```

**`purge_cdn` defaults to `True`** — `def delete_from_disk(self, purge_cdn=True)` (`app/models.py`). So the community path's bare call passes exactly what the site path passes explicitly, and the two are behaviourally identical. **Assert `calls[0][1] is True`**, and say in the docstring that the asymmetry is in the spelling, not the effect. The spec originally claimed a behavioural difference and was wrong; that correction is itself registered by Task 11.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 28 passed.

```bash
git add tests/test_ap_moderation.py
git commit -m "test: cover community ban data removal and its CDN asymmetry"
```

---

### Task 8: `ban_user`

**Files:**
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: Task 1's helpers and `make_community_ban`.
- Produces: nothing later tasks need.

`ban_user(blocker, blocked, community, core_activity)` branches on `community is None` — instance-wide versus community.

- [ ] **Step 1: Write the community ban**

```python
def test_a_community_ban_creates_the_ban_row_and_flags_the_membership(
        app, db_session, monkeypatch):
    """`ban_user`'s community branch. It creates a CommunityBan, flags any
    existing CommunityMember as banned, and writes a modlog entry.

    The membership row is created BEFORE the call so the
    `if community_membership_record:` branch is reached -- on a user with no
    membership that branch is skipped, and the ban still succeeds.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    make_community_member(author, community)
    db.session.commit()

    ap_util.ban_user(moderator, author, community,
                     {'id': 'https://peer.example/activities/block/1', 'summary': 'spam'})

    ban = db.session.query(CommunityBan).filter_by(user_id=author.id,
                                                   community_id=community.id).first()
    assert ban is not None
    assert ban.reason == 'spam'
    assert ban.banned_by == moderator.id
    membership = db.session.query(CommunityMember).filter_by(
        user_id=author.id, community_id=community.id).first()
    assert membership.is_banned is True
    assert db.session.query(ModLog).filter_by(action='ban_user').count() == 1
```

Add `CommunityBan` and `CommunityMember` to the file's `from app.models import ...` line.

- [ ] **Step 2: PIN the existing-row guard's scope**

```python
def test_re_banning_in_a_community_writes_no_second_modlog_entry(
        app, db_session, monkeypatch):
    """PINS AN ASYMMETRY. In `ban_user`'s COMMUNITY branch the
    `if not existing:` guard wraps the ENTIRE body, including
    `add_to_modlog`. So re-banning an already-banned user is a total no-op.

    In the INSTANCE branch the equivalent guard wraps only the InstanceBan
    row creation, so a re-ban there still notifies and still writes a modlog
    entry. Same intent, two different scopes -- pinned by this test and its
    instance-side twin below.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    make_community_ban(author, community, banned_by=moderator, reason='first')

    ap_util.ban_user(moderator, author, community,
                     {'id': 'https://peer.example/activities/block/2', 'summary': 'second'})

    ban = db.session.query(CommunityBan).filter_by(user_id=author.id,
                                                   community_id=community.id).first()
    assert ban.reason == 'first'   # unchanged -- the whole body was skipped
    assert db.session.query(ModLog).count() == 0
```

- [ ] **Step 3: Write the instance ban**

```python
def test_an_instance_ban_creates_an_instance_ban_row_and_a_modlog_entry(
        app, db_session, monkeypatch):
    """`ban_user`'s instance branch, reached by passing `community=None`. It
    resolves the instance from `core_activity['target']`'s host via
    `find_instance_id`, which CREATES AND COMMITS a sparse Instance row for
    an unknown domain -- so the target host is one this test seeded, or the
    ban would attach to a row that appeared as a side effect.
    """
    site, instance, community, author, moderator = seed_moderation_scene()

    ap_util.ban_user(moderator, author, None,
                     {'id': 'https://peer.example/activities/block/1',
                      'target': 'https://peer.example/',
                      'summary': 'spam'})

    ban = db.session.query(InstanceBan).filter_by(user_id=author.id).first()
    assert ban is not None
    assert db.session.query(ModLog).filter_by(action='ban_user').count() == 1
```

Add `InstanceBan` to the model imports.

- [ ] **Step 4: PIN the instance branch's different guard scope**

```python
def test_re_banning_instance_wide_still_writes_a_second_modlog_entry(
        app, db_session, monkeypatch):
    """The twin of the community test above, and the finding. Here
    `if not existing_ban:` guards ONLY the InstanceBan creation: the modlog
    entry sits outside it, so a duplicate ban writes a second entry where the
    community branch writes none.

    One ban row, two modlog entries -- both asserted, because either alone
    would be consistent with the other branch's behaviour.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    activity = {'id': 'https://peer.example/activities/block/1',
                'target': 'https://peer.example/', 'summary': 'spam'}

    ap_util.ban_user(moderator, author, None, activity)
    ap_util.ban_user(moderator, author, None, activity)

    assert db.session.query(InstanceBan).filter_by(user_id=author.id).count() == 1
    assert db.session.query(ModLog).filter_by(action='ban_user').count() == 2
```

- [ ] **Step 5: Write the reason-truncation test**

```python
def test_a_ban_reason_longer_than_255_characters_is_shortened(
        app, db_session, monkeypatch):
    """Both branches pass the summary through `shorten_string(reason, 255)`.
    `CommunityBan.reason` is a String(256) column, so an untruncated reason
    would raise on commit rather than silently truncate -- which is why this
    is worth a test rather than an assumption.

    Note shorten_string does not return exactly 255 characters: it cuts to
    252 and appends an ellipsis. The assertion is on the length bound, not
    on an exact figure, and the ellipsis is asserted separately.
    """
    site, instance, community, author, moderator = seed_moderation_scene()

    ap_util.ban_user(moderator, author, community,
                     {'id': 'https://peer.example/activities/block/1',
                      'summary': 'x' * 400})

    ban = db.session.query(CommunityBan).filter_by(user_id=author.id,
                                                   community_id=community.id).first()
    assert len(ban.reason) <= 255
    assert ban.reason.endswith('…')
```

**Read `shorten_string` in `app/utils.py` before asserting the ellipsis character** — sub-project 7 found it returns 98 characters for a limit of 100, so confirm the exact behaviour and assert what it really does.

- [ ] **Step 6: Run and commit**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 33 passed.

```bash
git add tests/test_ap_moderation.py
git commit -m "test: cover both ban_user branches and pin their guard-scope difference"
```

---

### Task 9: `unban_user`, and pin the missing modlog entry

**Files:**
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: Task 1's helpers and `make_community_ban`.
- Produces: nothing later tasks need.

`unban_user` reads its reason from `core_activity['object']['summary']` and its target from `core_activity['object']['target']` — **one level deeper than `ban_user`**, because an `Undo` wraps the original activity. Build the fixtures accordingly.

- [ ] **Step 1: Write the community unban**

```python
def test_a_community_unban_removes_the_ban_and_clears_the_membership_flag(
        app, db_session, monkeypatch):
    """`unban_user`'s community branch. Note the activity shape: reason comes
    from `core_activity['object']['summary']`, one level deeper than
    `ban_user` reads it, because an Undo wraps the activity it reverses.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    make_community_ban(author, community, banned_by=moderator)
    membership = make_community_member(author, community)
    membership.is_banned = True
    db.session.commit()

    ap_util.unban_user(moderator, author, community,
                       {'id': 'https://peer.example/activities/undo/1',
                        'object': {'summary': 'appeal upheld'}})

    remaining = db.session.query(CommunityBan).filter_by(user_id=author.id,
                                                         community_id=community.id).first()
    assert remaining is None
    assert membership.is_banned is False
    assert db.session.query(ModLog).filter_by(action='unban_user').count() == 1
```

- [ ] **Step 2: PIN the missing instance modlog entry**

```python
def test_an_instance_unban_writes_no_modlog_entry(app, db_session, monkeypatch):
    """PINS A DEFECT. `unban_user`'s INSTANCE branch calls no
    `add_to_modlog` at all, while its community branch does and BOTH
    branches of `ban_user` do. So an instance-wide ban is recorded in the
    moderation log and its reversal is not.

    The unban itself is asserted to have worked, so this cannot pass by the
    call having failed: the InstanceBan row is gone and the modlog is empty.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    make_instance_ban(author, instance)

    ap_util.unban_user(moderator, author, None,
                       {'id': 'https://peer.example/activities/undo/1',
                        'object': {'target': 'https://peer.example/',
                                   'summary': 'appeal upheld'}})

    assert db.session.query(InstanceBan).filter_by(user_id=author.id).count() == 0
    assert db.session.query(ModLog).count() == 0
```

Add `make_instance_ban` to the factory imports.

- [ ] **Step 3: Write the notification branch**

```python
def test_a_community_unban_notifies_only_a_user_who_has_posted_there(
        app, db_session, monkeypatch):
    """`if community.has_poster(blocked):` -- a user who never posted in the
    community is unbanned silently. The source comment on the matching guard
    in `ban_user` explains why: mods can use bans to harass, so a ban
    notification to someone with no history there would itself be the
    harassment.

    Two users, one with a post and one without, in one test -- a single-user
    fixture could not tell the guard from an unconditional notify.
    """
    site, instance, community, author, moderator = seed_moderation_scene()
    lurker = make_user(instance, 'thelurker', local=True)
    make_post(community, author, None, title='a post')
    make_community_ban(author, community, banned_by=moderator)
    make_community_ban(lurker, community, banned_by=moderator)
    db.session.commit()

    activity = {'id': 'https://peer.example/activities/undo/1',
                'object': {'summary': ''}}
    ap_util.unban_user(moderator, author, community, activity)
    ap_util.unban_user(moderator, lurker, community, activity)

    notified = db.session.query(Notification).filter_by(user_id=author.id).count()
    not_notified = db.session.query(Notification).filter_by(user_id=lurker.id).count()
    assert notified == 1
    assert not_notified == 0
```

**Read `Community.has_poster`** (`app/models.py`) before building the fixture — if it counts replies as well as posts, or checks a different relationship, follow what it actually reads.

- [ ] **Step 4: Run the tests, then mutation-test the `has_poster` guard**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 36 passed.

Drop `if community.has_poster(blocked):` so the notify runs unconditionally — `test_a_community_unban_notifies_only_a_user_who_has_posted_there` must fail on the lurker's count. Report the kill and its type. Restore and verify the diff is empty.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ap_moderation.py
git commit -m "test: cover both unban_user branches and pin the missing instance modlog"
```

---

### Task 10: Fix the write to a non-existent column

**Files:**
- Modify: `app/activitypub/util.py`
- Modify: `tests/test_ap_moderation.py`

**Interfaces:**
- Consumes: `test_a_site_ban_does_not_zero_the_users_reply_count` from Task 6.
- Produces: nothing later tasks need.

**This is the only task in the sub-project that changes production code.**

`site_ban_remove_data` contains `blocked.reply_count = 0`. `User` has no `reply_count` column — it declares `post_count` and `post_reply_count`, and `reply_count` belongs to `Post`. The statement sets a plain Python attribute, never persists, and never raises. The correct target is `post_reply_count`, which is what `community_ban_remove_data` decrements.

- [ ] **Step 1: Invert the pin**

Rename `test_a_site_ban_does_not_zero_the_users_reply_count` to
`test_a_site_ban_zeroes_the_users_reply_count`, change the assertion from
`author.post_reply_count == 5` to `== 0`, and rewrite the docstring so it
describes the fix rather than a live defect — keeping the explanation of what
was wrong, in the past tense, since that is why the test exists.

- [ ] **Step 2: Run it and watch it FAIL**

Run: `./run_tests.sh tests/test_ap_moderation.py::test_a_site_ban_zeroes_the_users_reply_count -q`
Expected: FAIL, `assert 5 == 0`. **Quote the failure verbatim in your report** — that output is the proof the test discriminates.

- [ ] **Step 3: Apply the fix**

Locate `site_ban_remove_data` by content and change `blocked.reply_count = 0` to `blocked.post_reply_count = 0`. Change nothing else.

- [ ] **Step 4: Run the file**

Run: `./run_tests.sh tests/test_ap_moderation.py -q`
Expected: 36 passed.

- [ ] **Step 5: Mutation-prove the fix**

Restore `blocked.reply_count = 0`, confirm the named test fails, restore the fix. Report the kill and whether it was an assertion failure or a crash. **It should be an assertion kill** — the mutant writes a harmless Python attribute rather than raising, which is exactly why the bug survived in production.

- [ ] **Step 6: Check the four existing suites still pass**

This fix changes a stored number. `tests/test_inbox_dispatch_block.py` drives `site_ban_remove_data` through the dispatcher. Run it:

Run: `./run_tests.sh tests/test_inbox_dispatch_block.py -q`
Expected: all pass. If any test asserted the old broken value, **report it rather than editing it** — a test that encoded the bug is a finding, and the controller will rule on whether it is a pin or an oversight.

- [ ] **Step 7: Audit docstrings falsified by the fix**

Grep `tests/test_ap_moderation.py` for `reply_count`, `does not`, `never`, and `defect`, and check every hit. `test_a_community_ban_decrements_the_users_real_reply_counter`'s docstring draws a contrast with "site_ban_remove_data's broken line" — that contrast is now historical and must read as such.

- [ ] **Step 8: Commit**

```bash
git add app/activitypub/util.py tests/test_ap_moderation.py
git commit -m "fix: zero the real reply counter when a site ban removes data"
```

---

### Task 11: Register the findings, record the harness facts, raise the floor

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `coverage_floors.ini`

**Interfaces:**
- Consumes: every earlier task's findings, via the SDD ledger.
- Produces: nothing.

**Write no tests and change no production code in this task.**

- [ ] **Step 1: Read the sources**

Read the SDD ledger at `.superpowers/sdd/2026-09-03-coverage-moderation-12/progress.md` in full — every task's outcome, every reviewer verdict, every controller ruling, including defects found but not authorised to fix. Then the per-task reports in the same directory, and the spec. **Register what was found, not what was predicted**; where a prediction was falsified, the falsification is itself worth recording.

- [ ] **Step 2: Write the register entries, numbered from D200**

Match the surrounding entries' house style — read several existing D-entries first. Each states the defect, where it lives (by content, with a line citation correct **at your commit**), how it was found, whether it was fixed or registered, and if registered, **why not fixed**.

At minimum, and the ledger will have more:

1. **The delete/restore cycle permanently loses `community.post_reply_count` and `post.reply_count_cross_posted`** — proved by a round-trip test, registered because correcting a counter changes numbers users already see and the historical drift is unrepairable.
2. **`site_ban_remove_data` wrote to a non-existent `User.reply_count`** — FIXED, with its commit, and record that SQLAlchemy accepted the assignment silently, which is why it survived.
3. **`restore_post_or_comment` takes no redis locks** where `delete_post_or_comment` wraps every one of the same counter mutations in one.
4. **`restore_post_or_comment`'s cross-post guard drops a conjunct** — `if to_restore.url:` against delete's `if to_delete.url and to_delete.cross_posts is not None:`.
5. **`unban_user`'s instance branch writes no modlog entry**, while its community branch and both `ban_user` branches do.
6. **`ban_user`'s existing-row guard has different scope in its two branches** — community wraps the whole body, instance wraps only the row creation.
7. **The `purge_cdn` call-site difference is cosmetic, not behavioural** — the parameter defaults to `True`, so both paths purge. Register it anyway: the asymmetric spelling invites a reader to infer a distinction that does not exist, and the spec asserted that wrong conclusion before the signature was checked.
8. **The two ban-removal functions use different query styles** — `db.session.query(...)` against the legacy `.query`, whose auto-deduplication masked a malformed join in D171.
9. **The four-disjunct authorisation guard is duplicated** between `delete_post_or_comment` and `restore_post_or_comment` rather than shared.

- [ ] **Step 3: Update BOTH live "Next free number" notes**

Find every occurrence of that phrase. The two carrying the campaign's current value must both be updated; the historical ones are frozen records — leave them alone. A previous sub-project updated only one and the register disagreed with itself.

- [ ] **Step 4: Add the harness facts to `tests/README.md`**

Read the file first and match its style; the numbered facts currently run to 55. Add only what is not already there. Candidates:

- **`log_incoming_ap` writes nothing unless `LOG_ACTIVITYPUB_TO_DB` is on**, and `config.py` defaults it to `False` with no override in `conftest.py` or `.env.test`. An `ActivityPubLog` assertion is vacuous by default and will pass against a function that deleted its logging entirely.
- **Assigning an undeclared attribute to a SQLAlchemy model instance is silent** — no persistence, no exception. `blocked.reply_count = 0` looked like a working line for as long as the function existed. Grep the model before trusting a counter name.
- **A counter test needs a seeded non-zero baseline**, because every counter column defaults to 0 and a decrement from 0 is indistinguishable from no decrement.
- **A pair of do/undo functions needs a round-trip test.** Asserting that the undo leaves a counter alone proves nothing unless the do moved it.
- Anything else in the ledger's rulings that generalises.

- [ ] **Step 5: Raise the floor**

`coverage_floors.ini`, `app/activitypub/util.py`: raise from 45 to the measured blended figure rounded down. **The controller supplies that figure — do not run a coverage run.**

- [ ] **Step 6: Verify every citation**

Task 10's fix may have shifted lines in `app/activitypub/util.py`. Verify every `app/*.py` line citation you wrote against current source. Stale citations are this campaign's most frequent defect.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 12's findings and raise the util.py floor"
```

---

## Self-review

**Spec coverage.** Each of the spec's six functions has at least one task: `delete_post_or_comment` Tasks 1-3, `restore_post_or_comment` Task 4, the round trip Task 5, `site_ban_remove_data` Tasks 6 and 10, `community_ban_remove_data` Task 7, `ban_user` Task 8, `unban_user` Task 9. Every row of the three asymmetry tables is reached: the locks (Task 4 docstring, registered Task 11), both counter rows (Tasks 4 and 5), the cross-post conjunct (Task 11), the `reply_count` column (Tasks 6 and 10), `purge_cdn` (Tasks 6 and 7), avatar/cover (Task 6), scope filter (Tasks 6 and 7), query style (Task 11), reason path (Task 9's fixture shape), guard scope (Task 8), modlog (Tasks 8 and 9), notify condition (Task 9), cache clearing (registered Task 11). The spec's nine success criteria map to Tasks 1-9 (1-3), Tasks 6 and 10 (4), Task 5 (5), Task 11 (6-8), and the controller's final run (9).

**Placeholder scan.** No "TBD", no "add appropriate error handling", no "similar to Task N". Every code step carries real code. Eight steps deliberately say "if X is not as described, read the model and report" — those are falsifiable checks against a stated expectation, not placeholders, and each names what to read.

**Type consistency.** `seed_moderation_scene()` returns the same five-tuple in every task that calls it. `_double_file_deletion(monkeypatch)` returns `(file_id, purge_cdn)` tuples in Tasks 1, 6 and 7. `make_community_ban(user, community, banned_by=None, reason='', ban_until=None)` is called with that signature in Tasks 8 and 9. `test_a_site_ban_does_not_zero_the_users_reply_count` is the name Task 6 produces and Task 10 consumes, and Task 10 states the new name explicitly.

**Two gaps found and fixed inline.** A drafted `_enable_ap_logging` helper was removed: no task called it, and the fact it encoded — that `log_incoming_ap` writes nothing unless `LOG_ACTIVITYPUB_TO_DB` is set — is already carried by "Facts every task needs" and is better served by every test asserting the real effect instead of a log row. And Task 2's mutation table originally listed only the four top-level disjuncts; the second disjunct is itself a conjunction whose two halves need their own kills, so the table now names them and Step 4 carries the analysis the implementer must do.

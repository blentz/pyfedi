# Sub-project 5d: the inbox dispatcher's Undo arm — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the `Undo` arm of `process_inbox_request` from 1 executed statement of 160 to full statement coverage across all seven sub-types, and fix the four defects the spec names.

**Architecture:** Tests drive `process_inbox_request` directly through `dispatch()`, seeding rows with `tests/factories.py` and doubling each delegate at its binding site on `app.activitypub.routes`. Three new test files split by sub-type affinity. Four production fixes, each written test-first and committed apart from any test-only commit.

**Tech Stack:** pytest, pytest-timeout, respx, fakeredis, SQLAlchemy, Flask, podman-compose (`./run_tests.sh`).

**Spec:** `docs/superpowers/specs/2026-08-31-coverage-inbox-undo-5d-design.md`

## Global Constraints

- No production change outside the four fixes named in the spec. Everything else found is **registered, not corrected**.
- Each fix is a **separate commit** from any test-only commit, and is proved by a mutation that fails a named test.
- Findings are numbered from **D104** in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`; the register's index note is updated in the same change that takes the numbers.
- The coverage floor in `coverage_floors.ini` rises to the measured blended figure **rounded down**, and only ever rises.
- **Locate every code target by content, not by the line numbers in this plan.** They are navigational aids and will drift — Fixes 2 and 3 change line counts inside the arm.
- No assertion may rest on a column's declared default. Seed an explicit contrary baseline first.
- Every guard is mutation-tested with **each conjunct dropped separately**, each killed by a distinct named test. A kill by `respx.models.AllMockedAssertionError` is an **infrastructure kill, not behavioural** — re-run with the fetch served before concluding a guard is load-bearing.
- The implementer never runs the full suite. The controller measures and supplies coverage figures; a task runs only its own file.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/factories.py` | gains `make_chat_message`; everything else it needs exists |
| `tests/test_inbox_dispatch_undo_follow.py` | Undo/Follow: community, feed, user, unfound target |
| `tests/test_inbox_dispatch_undo_content.py` | Undo/Delete, Like/Dislike, Announce, ChooseAnswer, fall-through |
| `tests/test_inbox_dispatch_undo_moderation.py` | Undo/Lock and Undo/Block |
| `app/activitypub/routes.py` | the four fixes only |
| `tests/README.md` | harness facts this slice establishes |
| `coverage_floors.ini` | floor raise |

**Shared helpers already available — do not rebuild them:**

- `dispatch(activity, store_ap_json=True)` — `tests/test_inbox_dispatch_preamble.py`
- `record_moderation(monkeypatch, *names)` — `tests/test_inbox_dispatch_lock_delete.py`
- `inbox_activity(actor, *, activity_type='Like', object_uri=None, **fields)` — `tests/factories.py`. `**fields` is applied last, so passing `object={...}` overrides the default string `object`, which is how every Undo activity in this slice is built.

**Seeding a community — get this right or nothing resolves.** `make_community`
hardcodes `instance_id=1` and `user_id=1`, so those rows must exist first.
`seed_community_owner(domain='peer.example') -> Instance` creates them and
returns the Instance; it takes a **domain string, not a community**, and it
calls `make_instance` itself, so do not also call `make_instance` for the same
domain. Moderator status is granted separately. The established pattern, copied
from `tests/test_inbox_dispatch_lock_delete.py`:

```python
instance = seed_community_owner('peer.example')   # instance id 1 + local owner user id 1
community = make_community(host='peer.example')
mod = make_user(instance, 'mod')
make_community_member(mod, community, is_moderator=True)
```

`make_feed(instance, name='peerfeed', public=False, local=False, with_keys=False)`
takes the **instance first** and needs no community seeding.

**Delegate signatures**, for writing doubles that accept the right shape:

```python
undo_vote(comment, post, target_ap_id, user)                       # app/activitypub/util.py:3532
undo_boost(target_ap_id: str, user: User) -> Union[Post, None]     # :3568
announce_target_uri(activity: dict) -> Union[str, None]            # :3859
find_liked_object(ap_id) -> Union[Post, PostReply, None]           # :2024
restore_post_or_comment(restorer, to_restore, store_ap_json, request_json, reason)  # :2268
unban_user(blocker, blocked, community, core_activity)             # :2510
```

---

### Task 1: `make_chat_message` factory

**Files:**
- Modify: `tests/factories.py`
- Test: `tests/test_factories_chat.py` (create)

**Interfaces:**
- Produces: `make_chat_message(sender, recipient, ap_id, *, body='a message', deleted=False) -> ChatMessage`

`ChatMessage` is at `app/models.py:283`. Its `conversation_id` is nullable, so no `Conversation` is needed.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_factories_chat.py"""
from tests.factories import make_chat_message, make_instance, make_user


def test_make_chat_message_builds_a_row_reachable_by_ap_id_and_sender(app, db_session):
    """Undo/Delete's PM-restore branch queries ChatMessage by ap_id AND
    sender_id together, so both must be set for the factory to be useful.
    `deleted` is asserted at an explicitly-passed True rather than its column
    default, so this proves the parameter works rather than restating the
    default.
    """
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)

    message = make_chat_message(sender, recipient, 'https://peer.example/pm/1', deleted=True)

    assert message.id is not None
    assert message.sender_id == sender.id
    assert message.recipient_id == recipient.id
    assert message.ap_id == 'https://peer.example/pm/1'
    assert message.deleted is True
    assert message.conversation_id is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./run_tests.sh tests/test_factories_chat.py -q`
Expected: FAIL, `ImportError: cannot import name 'make_chat_message'`

- [ ] **Step 3: Add the factory**

Add `ChatMessage` to the `app.models` import list at the top of `tests/factories.py`, then:

```python
def make_chat_message(sender: User, recipient: User, ap_id: str, *,
                      body: str = 'a message', deleted: bool = False) -> ChatMessage:
    """A ChatMessage for Undo/Delete's PM-restore branch, which queries
    `ChatMessage` by `ap_id` and `sender_id` together (the branch reached when
    `find_liked_object` finds no post or comment for the id).

    conversation_id is left NULL deliberately: the column is nullable
    (app/models.py:287) and nothing on the restore path reads it, so building a
    Conversation would be scaffolding no test asserts on.
    """
    message = ChatMessage(
        sender_id=sender.id,
        recipient_id=recipient.id,
        body=body,
        ap_id=ap_id,
        deleted=deleted,
    )
    db.session.add(message)
    db.session.commit()
    return message
```

- [ ] **Step 4: Run it and watch it pass**

Run: `./run_tests.sh tests/test_factories_chat.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/factories.py tests/test_factories_chat.py
git commit -m "test: add a ChatMessage factory for Undo/Delete's PM branch"
```

---

### Task 2: Undo/Follow — the community branch

**Files:**
- Create: `tests/test_inbox_dispatch_undo_follow.py`
- Test target: the `isinstance(target, Community)` branch of Undo/Follow

**Interfaces:**
- Consumes: `dispatch`, `inbox_activity`, `make_community_member`, `make_community_join_request`
- Produces: `undo_follow_activity(actor, target_ap_id)` helper used by Tasks 3

The branch deletes a `CommunityMember` and a `CommunityJoinRequest`, decrements `community.subscriptions_count`, stamps `community.last_active` and `user.last_seen`, invalidates `community_membership`, and logs `APLOG_UNDO_FOLLOW` / success. Member and join request are **two independent `if`s** — either can be absent.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_inbox_dispatch_undo_follow.py"""
from datetime import timedelta

from app import db
from app.models import ActivityPubLog, CommunityJoinRequest, CommunityMember, utcnow
from tests.factories import (inbox_activity, make_community, make_community_join_request,
                             make_community_member, make_instance, make_user,
                             seed_community_owner)
from tests.test_inbox_dispatch_preamble import dispatch


def undo_follow_activity(actor, target_ap_id):
    """An Undo whose inner object is a Follow of `target_ap_id`.

    `inbox_activity` applies **fields last, so passing `object=` replaces its
    default string object with the dict the arm requires -- the arm dispatches
    on `core_activity['object']['type']`, which a string could not satisfy.
    """
    return inbox_activity(actor, activity_type='Undo',
                          object={'type': 'Follow', 'object': target_ap_id})


def test_undo_follow_of_a_community_removes_membership_and_join_request(app, db_session, monkeypatch):
    """Both `if member:` and `if join_request:` fire. subscriptions_count is
    seeded to 5 rather than left at its default so the decrement to 4 is
    evidence of the write, not of a default sitting there unexamined.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    follower = make_user(instance, 'follower')
    community = make_community(host='peer.example')
    make_community_member(follower, community)
    make_community_join_request(follower, community)
    community.subscriptions_count = 5
    stale = utcnow() - timedelta(days=3)
    community.last_active = stale
    follower.last_seen = stale
    db.session.commit()
    community_id, follower_id = community.id, follower.id

    dispatch(undo_follow_activity(follower, community.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(CommunityMember).filter_by(
        user_id=follower_id, community_id=community_id).first() is None
    assert db.session.query(CommunityJoinRequest).filter_by(
        user_id=follower_id, community_id=community_id).first() is None
    fresh = db.session.get(type(community), community_id)
    assert fresh.subscriptions_count == 4
    assert fresh.last_active > stale
    assert db.session.get(type(follower), follower_id).last_seen > stale

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_follow_of_a_community_with_no_membership_leaves_the_count_alone(app, db_session, monkeypatch):
    """`if member:` is false, so the decrement and both timestamps are skipped
    -- proving the guard is load-bearing rather than decoration. The join
    request is still present and still deleted, which is what distinguishes
    the two independent `if`s from a single combined one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    follower = make_user(instance, 'follower')
    community = make_community(host='peer.example')
    make_community_join_request(follower, community)
    community.subscriptions_count = 5
    stale = utcnow() - timedelta(days=3)
    community.last_active = stale
    db.session.commit()
    community_id, follower_id = community.id, follower.id

    dispatch(undo_follow_activity(follower, community.ap_profile_id))

    db.session.expire_all()
    fresh = db.session.get(type(community), community_id)
    assert fresh.subscriptions_count == 5          # untouched
    assert fresh.last_active == stale              # untouched
    assert db.session.query(CommunityJoinRequest).filter_by(
        user_id=follower_id, community_id=community_id).first() is None

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_follow_of_a_community_with_no_join_request_still_removes_membership(app, db_session, monkeypatch):
    """The mirror of the test above: `if join_request:` false, `if member:`
    true. Together the pair kills either `if` being dropped.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    follower = make_user(instance, 'follower')
    community = make_community(host='peer.example')
    make_community_member(follower, community)
    community.subscriptions_count = 5
    db.session.commit()
    community_id, follower_id = community.id, follower.id

    dispatch(undo_follow_activity(follower, community.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(CommunityMember).filter_by(
        user_id=follower_id, community_id=community_id).first() is None
    assert db.session.get(type(community), community_id).subscriptions_count == 4

    log = ActivityPubLog.query.one()
    assert log.result == 'success'
```

- [ ] **Step 2: Run and watch them fail or pass for the right reason**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_follow.py -q`
Expected: PASS. These are coverage tests against existing behaviour, so a failure here is a genuine finding — record it in the report rather than adjusting the assertion to match.

- [ ] **Step 3: Mutation-test both guards**

Drop `if member:` (de-indent its body) and confirm `test_undo_follow_of_a_community_with_no_membership_leaves_the_count_alone` fails. Restore. Drop `if join_request:` and confirm `test_undo_follow_of_a_community_with_no_join_request_still_removes_membership` fails. Restore. Record both kills in the report.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_undo_follow.py
git commit -m "test: cover Undo/Follow's community branch"
```

---

### Task 3: Undo/Follow — feed, user, and unfound target

**Files:**
- Modify: `tests/test_inbox_dispatch_undo_follow.py`

**Interfaces:**
- Consumes: `undo_follow_activity` from Task 2, `make_feed`, `make_feed_member`, `make_feed_join_request`, `make_follow`

Three remaining branches. The feed branch mirrors the community one but does **not** stamp any timestamp. The user branch deletes a `UserFollower` filtered on `is_accepted=True` and logs **only when a follower row existed** — a missing row returns silently with no log at all. `if not target:` logs failure `'Unfound target'`.

- [ ] **Step 1: Write the failing tests**

```python
def test_undo_follow_of_a_feed_removes_membership_and_join_request(app, db_session, monkeypatch):
    """The feed branch mirrors the community branch but stamps NO timestamps --
    asserted explicitly below, because the omission is the interesting
    difference between the two branches.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    follower = make_user(instance, 'follower')
    feed = make_feed(instance, 'news')
    make_feed_member(follower, feed)
    make_feed_join_request(follower, feed)
    feed.subscriptions_count = 5
    stale = utcnow() - timedelta(days=3)
    follower.last_seen = stale
    db.session.commit()
    feed_id, follower_id = feed.id, follower.id

    dispatch(undo_follow_activity(follower, feed.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(FeedMember).filter_by(user_id=follower_id, feed_id=feed_id).first() is None
    assert db.session.query(FeedJoinRequest).filter_by(user_id=follower_id, feed_id=feed_id).first() is None
    assert db.session.get(type(feed), feed_id).subscriptions_count == 4
    assert db.session.get(type(follower), follower_id).last_seen == stale  # NOT stamped

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_follow_of_a_local_user_deletes_an_accepted_follower(app, db_session, monkeypatch):
    """The user branch. `make_follow(local_user, remote_user, is_accepted=True)`
    matches the filter the branch applies.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    remote = make_user(instance, 'remote')
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()
    make_follow(local, remote, is_accepted=True)
    local_id, remote_id = local.id, remote.id

    dispatch(undo_follow_activity(remote, local.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(UserFollower).filter_by(
        local_user_id=local_id, remote_user_id=remote_id).first() is None

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_follow_of_a_user_with_a_pending_follow_logs_nothing(app, db_session, monkeypatch):
    """`is_accepted=True` is part of the filter, so a PENDING follow does not
    match and the branch returns having logged NOTHING -- there is no
    log_incoming_ap call outside the `if follower:` block. Asserted with
    LOG_ACTIVITYPUB_TO_DB explicitly True, so the zero is a real silence and
    not an artifact of logging being off.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    remote = make_user(instance, 'remote')
    local = make_user(None, 'local', local=True)
    local.ap_profile_id = f"{app.config['SERVER_URL']}/u/local".lower()
    db.session.commit()
    make_follow(local, remote, is_accepted=False)
    local_id, remote_id = local.id, remote.id

    dispatch(undo_follow_activity(remote, local.ap_profile_id))

    db.session.expire_all()
    assert db.session.query(UserFollower).filter_by(
        local_user_id=local_id, remote_user_id=remote_id).first() is not None  # survives
    assert ActivityPubLog.query.count() == 0


def test_undo_follow_of_an_unresolvable_target_logs_failure(app, db_session, monkeypatch):
    """`find_actor_or_create_cached` is doubled to return None so the arm
    reaches `if not target:`. Doubling is necessary rather than convenient: an
    undoubled lookup of an unknown URL attempts a real fetch, which
    block_outbound_http turns into a respx error -- an INFRASTRUCTURE failure
    that would look like a behavioural one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    follower = make_user(instance, 'follower')
    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached',
                        lambda *args, **kwargs: None)

    dispatch(undo_follow_activity(follower, 'https://peer.example/c/gone'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unfound target'
```

Add to the imports at the top of the file:

```python
from app.models import (ActivityPubLog, CommunityJoinRequest, CommunityMember,
                        FeedJoinRequest, FeedMember, UserFollower, utcnow)
from app.activitypub import routes as activitypub_routes
from tests.factories import (inbox_activity, make_community, make_community_join_request,
                             make_community_member, make_feed, make_feed_join_request,
                             make_feed_member, make_follow, make_instance, make_user,
                             seed_community_owner)
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_follow.py -q`
Expected: PASS

- [ ] **Step 3: Mutation-test the `is_accepted` filter**

Remove `is_accepted=True` from the `UserFollower` filter and confirm `test_undo_follow_of_a_user_with_a_pending_follow_logs_nothing` fails. Restore, record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_undo_follow.py
git commit -m "test: cover Undo/Follow's feed, user and unfound-target paths"
```

---

### Task 4: Undo/Delete — the restore path

**Files:**
- Create: `tests/test_inbox_dispatch_undo_content.py`

**Interfaces:**
- Produces: `undo_activity(actor, inner_type, inner_object, **inner)` helper reused by Tasks 5, 6, 7 and 11

Three outcomes: content found and already restored (IGNORED), content found and deleted (restored, then announced unless `announced`), content not found (falls to the PM branch, Task 5).

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_inbox_dispatch_undo_content.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, ChatMessage
from tests.factories import (inbox_activity, make_community, make_chat_message, make_instance,
                             make_post, make_post_reply, make_user, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def undo_activity(actor, inner_type, inner_object, **inner):
    """An Undo wrapping an inner activity of `inner_type` about `inner_object`.

    `inbox_activity` applies **fields last, so `object=` replaces its default
    string object -- required, because the arm dispatches on
    `core_activity['object']['type']`.
    """
    obj = {'type': inner_type, 'object': inner_object}
    obj.update(inner)
    return inbox_activity(actor, activity_type='Undo', object=obj)


def test_undo_delete_restores_a_deleted_post_and_announces_it(app, db_session, monkeypatch):
    """The main restore path. `restore_post_or_comment` and
    `announce_activity_to_followers` are doubled at their binding site on the
    routes module -- routes imports the first by name and defines the second
    itself, and both patch the same way.

    The activity is NOT announced (no Announce wrapper), so `if not announced:`
    is true and the follower announce fires. Its sibling below proves the
    guard by sending the same Undo inside an Announce.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    dispatch(undo_activity(author, 'Delete', post.ap_id))

    assert len(calls['restore_post_or_comment']) == 1
    args, kwargs = calls['restore_post_or_comment'][0]
    restorer_arg, to_restore_arg = args[0], args[1]
    assert restorer_arg.id == author.id
    assert to_restore_arg.id == post.id
    assert len(calls['announce_activity_to_followers']) == 1


def test_undo_delete_of_content_that_is_not_deleted_is_ignored(app, db_session, monkeypatch):
    """`if not to_restore.deleted:` -- `deleted` is seeded explicitly False
    rather than left at its column default, so the IGNORED outcome is evidence
    about the guard rather than about the default.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = False
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    dispatch(undo_activity(author, 'Delete', post.ap_id))

    assert calls['restore_post_or_comment'] == []
    assert calls['announce_activity_to_followers'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Activity about local content which is already restored'


def test_undo_delete_passes_the_summary_as_the_reason(app, db_session, monkeypatch):
    """`reason` comes from the INNER object's 'summary', not the outer
    activity's -- asserted here by setting only the inner one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    dispatch(undo_activity(author, 'Delete', post.ap_id, summary='a good reason'))

    args, kwargs = calls['restore_post_or_comment'][0]
    assert args[4] == 'a good reason'


def test_undo_delete_without_a_summary_passes_an_empty_reason(app, db_session, monkeypatch):
    """The `else ''` half of the same conditional. Paired with the test above
    so neither half can be dropped without a failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    dispatch(undo_activity(author, 'Delete', post.ap_id))

    args, kwargs = calls['restore_post_or_comment'][0]
    assert args[4] == ''
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_content.py -q`
Expected: PASS

- [ ] **Step 3: Mutation-test the summary conditional**

Replace the `reason` conditional with a bare `''` and confirm `test_undo_delete_passes_the_summary_as_the_reason` fails. Restore, record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_undo_content.py
git commit -m "test: cover Undo/Delete's restore path"
```

---

### Task 5: Undo/Delete — the PM fallback and both object shapes

**Files:**
- Modify: `tests/test_inbox_dispatch_undo_content.py`

**Interfaces:**
- Consumes: `undo_activity` from Task 4, `make_chat_message` from Task 1

When `find_liked_object` returns nothing the arm queries `ChatMessage` by `ap_id` **and** `sender_id`. The `sender_id` half is the interesting one: a PM sent by somebody else is not restored. The arm also reads `ap_id` from either a string object (lemmy) or `object['id']` (kbin).

- [ ] **Step 1: Write the failing tests**

```python
def test_undo_delete_restores_a_deleted_private_message(app, db_session, monkeypatch):
    """The PM fallback, reached because no post or comment has this ap_id.
    `deleted` is seeded True so flipping it to False is a real observation.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)
    message = make_chat_message(sender, recipient, 'https://peer.example/pm/1', deleted=True)
    message_id = message.id

    dispatch(undo_activity(sender, 'Delete', 'https://peer.example/pm/1'))

    db.session.expire_all()
    assert db.session.get(ChatMessage, message_id).deleted is False
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert 'https://peer.example/pm/1' in log.exception_message


def test_undo_delete_does_not_restore_a_private_message_sent_by_someone_else(app, db_session, monkeypatch):
    """`sender_id=restorer.id` is half the filter. A different actor undoing
    the delete finds nothing, so the message stays deleted and NOTHING is
    logged -- there is no log call on the not-found path at all.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    interloper = make_user(instance, 'interloper')
    recipient = make_user(None, 'recipient', local=True)
    message = make_chat_message(sender, recipient, 'https://peer.example/pm/1', deleted=True)
    message_id = message.id

    dispatch(undo_activity(interloper, 'Delete', 'https://peer.example/pm/1'))

    db.session.expire_all()
    assert db.session.get(ChatMessage, message_id).deleted is True  # untouched
    assert ActivityPubLog.query.count() == 0


def test_undo_delete_reads_the_kbin_dict_object_shape(app, db_session, monkeypatch):
    """`isinstance(..., str)` is false, so ap_id comes from `object['id']`.
    A kbin-shaped inner object is a dict carrying its own 'id'.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)
    message = make_chat_message(sender, recipient, 'https://peer.example/pm/1', deleted=True)
    message_id = message.id

    dispatch(undo_activity(sender, 'Delete', {'id': 'https://peer.example/pm/1',
                                              'type': 'Note'}))

    db.session.expire_all()
    assert db.session.get(ChatMessage, message_id).deleted is False
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_content.py -q`
Expected: PASS

- [ ] **Step 3: Mutation-test the sender filter**

Drop `sender_id=restorer.id` from the `ChatMessage` filter and confirm `test_undo_delete_does_not_restore_a_private_message_sent_by_someone_else` fails. Restore, record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_undo_content.py
git commit -m "test: cover Undo/Delete's PM fallback and both object shapes"
```

---

### Task 6: Undo/Like, Undo/Dislike and Undo/Announce

**Files:**
- Modify: `tests/test_inbox_dispatch_undo_content.py`

Both sub-types are thin: a delegate call and a two-way log. `undo_vote(comment, post, target_ap_id, user)` is called with `comment` and `post` both `None`. The vote path announces with `can_batch=True`; the boost path never announces.

- [ ] **Step 1: Write the failing tests**

```python
def test_undo_like_calls_undo_vote_with_both_objects_none_and_announces_batchable(app, db_session, monkeypatch):
    """`post = comment = None` immediately before the call, so the delegate
    receives two Nones and resolves the target itself from the ap_id. The
    announce is made with can_batch=True, which is asserted rather than merely
    counted because it is a federation-behaviour choice, not an implementation
    detail.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    voter = make_user(instance, 'voter')
    community = make_community(host='peer.example')
    post = make_post(community, voter, 'https://peer.example/post/1')

    calls = record_moderation(monkeypatch, 'announce_activity_to_followers')
    monkeypatch.setattr(activitypub_routes, 'undo_vote',
                        lambda comment, post_, target_ap_id, user: post)

    dispatch(undo_activity(voter, 'Like', 'https://peer.example/post/1'))

    assert len(calls['announce_activity_to_followers']) == 1
    args, kwargs = calls['announce_activity_to_followers'][0]
    assert kwargs.get('can_batch') is True

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_dislike_takes_the_same_branch_as_undo_like(app, db_session, monkeypatch):
    """The guard is `== 'Like' or == 'Dislike'`. This test covers the second
    disjunct; the test above covers the first, so dropping either fails one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    voter = make_user(instance, 'voter')
    community = make_community(host='peer.example')
    post = make_post(community, voter, 'https://peer.example/post/1')

    record_moderation(monkeypatch, 'announce_activity_to_followers')
    monkeypatch.setattr(activitypub_routes, 'undo_vote',
                        lambda comment, post_, target_ap_id, user: post)

    dispatch(undo_activity(voter, 'Dislike', 'https://peer.example/post/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_like_of_an_unfound_object_logs_failure_with_the_uri(app, db_session, monkeypatch):
    """`undo_vote` returning None takes the else. The target uri is
    concatenated into the message, so the assertion pins it.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    voter = make_user(instance, 'voter')

    calls = record_moderation(monkeypatch, 'announce_activity_to_followers')
    monkeypatch.setattr(activitypub_routes, 'undo_vote',
                        lambda comment, post_, target_ap_id, user: None)

    dispatch(undo_activity(voter, 'Like', 'https://peer.example/post/404'))

    assert calls['announce_activity_to_followers'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unfound object https://peer.example/post/404'


def test_undo_announce_undoes_a_boost_via_the_outer_actor(app, db_session, monkeypatch):
    """The arm's own comment insists the actor comes from the SIGNED outer
    activity, never the inner object. The inner object here carries a
    DIFFERENT actor, and the assertion is that `undo_boost` receives the outer
    one -- which is the whole point of that comment.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    booster = make_user(instance, 'booster')
    impostor = make_user(instance, 'impostor')
    community = make_community(host='peer.example')
    post = make_post(community, booster, 'https://peer.example/post/1')

    seen = {}

    def fake_undo_boost(target_ap_id, user):
        seen['target'] = target_ap_id
        seen['user_id'] = user.id
        return post

    monkeypatch.setattr(activitypub_routes, 'undo_boost', fake_undo_boost)
    monkeypatch.setattr(activitypub_routes, 'announce_target_uri',
                        lambda activity: 'https://peer.example/post/1')

    dispatch(undo_activity(booster, 'Announce', 'https://peer.example/post/1',
                           actor=impostor.ap_profile_id))

    assert seen['user_id'] == booster.id      # outer actor, not the impostor
    assert seen['target'] == 'https://peer.example/post/1'
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_announce_of_an_unfound_post_is_ignored(app, db_session, monkeypatch):
    """`undo_boost` returning None takes the else, which logs IGNORED (not
    FAILURE, unlike the vote path a few lines above -- the two thin arms differ
    here and the pair of tests pins the difference).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    booster = make_user(instance, 'booster')

    monkeypatch.setattr(activitypub_routes, 'undo_boost', lambda target_ap_id, user: None)
    monkeypatch.setattr(activitypub_routes, 'announce_target_uri',
                        lambda activity: 'https://peer.example/post/404')

    dispatch(undo_activity(booster, 'Announce', 'https://peer.example/post/404'))

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert 'https://peer.example/post/404' in log.exception_message
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_content.py -q`
Expected: PASS

- [ ] **Step 3: Mutation-test the `can_batch` argument and the Dislike disjunct**

Remove `can_batch=True` and confirm the first test fails. Restore. Remove `or core_activity['object']['type'] == 'Dislike'` and confirm `test_undo_dislike_takes_the_same_branch_as_undo_like` fails. Restore. Record both.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_undo_content.py
git commit -m "test: cover Undo/Like, Undo/Dislike and Undo/Announce"
```

---

### Task 7: Undo/ChooseAnswer, and the `'Unmatched activity'` fall-through

**Files:**
- Modify: `tests/test_inbox_dispatch_undo_content.py`

ChooseAnswer clears `post_reply.answer` inside `with redis_client.lock(...)`. **fakeredis cannot serve a redis-py lock** (`tests/conftest.py:429-444`); use the narrower `app.redis_client` double from `tests/test_inbox_dispatch_votes.py`, whose `.lock(...)` is a real no-op context manager. Read that file's fixture before writing this task.

- [ ] **Step 1: Write the failing tests**

```python
def test_undo_choose_answer_clears_the_answer_flag(app, db_session, monkeypatch):
    """`answer` is seeded True so clearing it to False is a real write.

    The arm wraps the write in `with redis_client.lock(...)`. This suite's
    fakeredis instance cannot serve a redis-py lock (tests/conftest.py:429-444),
    so `app.redis_client` is replaced with the narrower double
    tests/test_inbox_dispatch_votes.py established, whose `.lock(...)` is a
    genuine no-op context manager. Do NOT change the redis_double fixture --
    it patches the right attribute; the limitation is in fakeredis.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    install_lock_only_redis(monkeypatch)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.answer = True
    db.session.commit()
    reply_id = reply.id

    dispatch(undo_activity(author, 'ChooseAnswer', 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).answer is False
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_choose_answer_for_an_unknown_reply_logs_nothing(app, db_session, monkeypatch):
    """`if post_reply:` is false, so the arm returns having logged nothing --
    asserted with LOG_ACTIVITYPUB_TO_DB explicitly True so the zero is real.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    install_lock_only_redis(monkeypatch)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')

    dispatch(undo_activity(author, 'ChooseAnswer', 'https://peer.example/comment/404'))

    assert ActivityPubLog.query.count() == 0


def test_an_undo_of_an_unrecognised_type_falls_through_to_monitor(app, db_session, monkeypatch):
    """The eighth path: an inner type matching none of the seven sub-types
    reaches the arm's final log_incoming_ap. 'Move' is chosen deliberately --
    it is a real activity type this dispatcher handles at the TOP level, so
    the test proves the Undo arm does not accidentally reuse the outer
    dispatcher's handling for it.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    actor = make_user(instance, 'actor')

    dispatch(undo_activity(actor, 'Move', 'https://peer.example/u/someone'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Unmatched activity'
```

Add the redis helper near the top of the file, copying the shape from `tests/test_inbox_dispatch_votes.py` rather than inventing one:

```python
def install_lock_only_redis(monkeypatch):
    """Replace `app.redis_client` with a double whose only capability is
    `.lock(...)` as a no-op context manager. See
    tests/test_inbox_dispatch_votes.py, which established this and explains
    why redis_double's fakeredis cannot serve a redis-py lock.
    """
    import contextlib

    import app as app_module

    class LockOnlyRedis:
        def lock(self, *args, **kwargs):
            return contextlib.nullcontext()

    monkeypatch.setattr(app_module, 'redis_client', LockOnlyRedis(), raising=False)
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_content.py -q`
Expected: PASS. If the ChooseAnswer tests fail inside the lock, re-read `tests/test_inbox_dispatch_votes.py` — the double must be installed on the same attribute the arm resolves (`from app import redis_client` **inside** the function body, so patching `app.redis_client` is what takes effect).

- [ ] **Step 3: Commit**

```bash
git add tests/test_inbox_dispatch_undo_content.py
git commit -m "test: cover Undo/ChooseAnswer and the unmatched-type fall-through"
```

---

### Task 8: Undo/Lock — pin the three defects before fixing them

**Files:**
- Create: `tests/test_inbox_dispatch_undo_moderation.py`

This task writes tests that **assert the broken behaviour**, so the fixes in Task 9 have something to invert. Every test here is rewritten in Task 9; that is deliberate and is how the fixes are proved.

- [ ] **Step 1: Write tests pinning current behaviour**

```python
"""tests/test_inbox_dispatch_undo_moderation.py"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, InstanceRole, User, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member,
                             make_instance, make_post, make_post_reply, make_user,
                             seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def undo_lock_activity(actor, target_ap_id, **outer):
    """An Undo wrapping a Lock of `target_ap_id`."""
    return inbox_activity(actor, activity_type='Undo',
                          object={'type': 'Lock', 'object': target_ap_id}, **outer)


def _seed_lockable_post(host='peer.example'):
    """A community whose owner is also a moderator, plus a locked post."""
    instance = seed_community_owner(host)
    mod = make_user(instance, 'mod')
    community = make_community(host=host)
    make_community_member(mod, community, is_moderator=True)
    author = make_user(instance, 'author')
    post = make_post(community, author, f'https://{host}/post/1')
    post.comments_enabled = False
    db.session.commit()
    return instance, mod, community, author, post


def test_a_successful_post_unlock_also_logs_a_contradictory_failure(app, db_session, monkeypatch):
    """PINS FIX 3's defect. The `else` binds to `if post_reply:`, not to the
    pair, so a post unlock that SUCCEEDED logs APLOG_SUCCESS and then, because
    post_reply is None, immediately logs FAILURE 'Unlock: post not found' for
    the same activity. Two rows, contradicting each other.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    record_moderation(monkeypatch, 'add_to_modlog')
    post_id = post.id

    dispatch(undo_lock_activity(mod, post.ap_id))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).comments_enabled is True

    logs = ActivityPubLog.query.order_by(ActivityPubLog.id).all()
    assert len(logs) == 2                       # the defect
    assert logs[0].result == 'success'
    assert logs[1].result == 'failure'
    assert logs[1].exception_message == 'Unlock: post not found'


def test_the_post_and_comment_url_branches_are_dead(app, db_session, monkeypatch):
    """PINS FIX 2's defect. `'/post/' in core_activity['object']` tests the
    DICT's keys, not the target string, so it can never match however the
    object id is spelled. Proved by giving the inner object an id containing
    '/comment/' while ALSO giving the dict a '/post/' key: if the test were
    against the string the '/comment/' branch would run; if against the dict,
    the '/post/' key would match. Neither happens -- the else fallback runs and
    resolves the reply -- which is only explicable if the operand is the dict
    and neither literal is among its keys.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()

    activity = inbox_activity(mod, activity_type='Undo',
                              object={'type': 'Lock', 'object': reply.ap_id})
    assert '/post/' not in activity['object']   # the dict has no such KEY
    assert '/comment/' not in activity['object']

    record_moderation(monkeypatch, 'add_to_modlog')

    with pytest.raises(AttributeError):
        dispatch(activity)


def test_unlocking_a_comment_crashes_on_a_none_post(app, db_session, monkeypatch):
    """PINS FIX 1's defect, the exact twin of D97 (fixed in b79f43f9 in the
    Lock arm's own comment branch). The reply IS unlocked and committed first,
    then `add_to_modlog(..., target_user=post.author, community=post.community)`
    dereferences `post`, which is None on this branch.

    add_to_modlog is deliberately NOT doubled here: doubling it would swallow
    the very dereference this test exists to observe, because the arguments are
    evaluated at the call site, before any double is entered. That is the
    subtlety this docstring exists to record.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()

    with pytest.raises(AttributeError):
        dispatch(undo_lock_activity(mod, reply.ap_id))


def test_unlocking_without_permission_logs_failure(app, db_session, monkeypatch):
    """The permission guard, which is NOT defective. A user who is neither
    moderator nor instance admin gets FAILURE and no unlock.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    outsider = make_user(instance, 'outsider')
    post_id = post.id

    dispatch(undo_lock_activity(outsider, post.ap_id))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).comments_enabled is False  # untouched
    logs = ActivityPubLog.query.order_by(ActivityPubLog.id).all()
    assert logs[0].result == 'failure'
    assert logs[0].exception_message == 'Unlock: Does not have permission'
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_moderation.py -q`
Expected: PASS. Every assertion above describes current behaviour. If one fails, the defect is not what the spec says it is — report that rather than editing the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/test_inbox_dispatch_undo_moderation.py
git commit -m "test: pin Undo/Lock's three defects as observed behaviour"
```

---

### Task 9: Undo/Lock — land the three fixes

**Files:**
- Modify: `app/activitypub/routes.py` (Undo/Lock only)
- Modify: `tests/test_inbox_dispatch_undo_moderation.py`

Fixes 1 and 2 **must land in the same commit** — fixing the membership test makes the `/comment/` branch live, which makes Fix 1's crash reachable by a second route. Fix 3 is a separate commit.

**Locate the code by content.** The target is the block starting `target_ap_id = core_activity['object']['object']` inside `if core_activity['object']['type'] == 'Lock':`.

- [ ] **Step 1: Invert the tests for Fixes 1 and 2**

Replace `test_the_post_and_comment_url_branches_are_dead` and `test_unlocking_a_comment_crashes_on_a_none_post` with:

```python
def test_the_comment_url_branch_selects_a_reply_directly(app, db_session, monkeypatch):
    """FIX 2. The membership test now runs against `target_ap_id`, the string,
    so an id containing '/comment/' selects PostReply WITHOUT first trying
    Post.get_by_ap_id. Proved by seeding a Post whose ap_id is identical to the
    reply's: if the else fallback were still running it would find that post
    first and unlock IT instead.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    decoy = make_post(community, author, 'https://peer.example/comment/1')
    decoy.comments_enabled = False
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()
    decoy_id, reply_id = decoy.id, reply.id

    record_moderation(monkeypatch, 'add_to_modlog')

    dispatch(undo_lock_activity(mod, 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is True
    assert db.session.get(type(decoy), decoy_id).comments_enabled is False  # decoy untouched


def test_unlocking_a_comment_records_the_reply_author_and_community(app, db_session, monkeypatch):
    """FIX 1, the twin of D97. The modlog entry now reads its author and
    community off `post_reply`, not off the always-None `post`. add_to_modlog
    is doubled so the arguments can be inspected -- which is only safe now that
    evaluating them no longer raises.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.replies_enabled = False
    db.session.commit()
    reply_id = reply.id

    calls = record_moderation(monkeypatch, 'add_to_modlog')

    dispatch(undo_lock_activity(mod, 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).replies_enabled is True

    assert len(calls['add_to_modlog']) == 1
    args, kwargs = calls['add_to_modlog'][0]
    assert args[0] == 'unlock_post_reply'
    assert kwargs['target_user'].id == author.id
    assert kwargs['community'].id == community.id
    assert kwargs['reply'].id == reply_id
```

- [ ] **Step 2: Run and watch them fail**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_moderation.py -q`
Expected: both new tests FAIL — the first because the else fallback still finds the decoy post, the second with `AttributeError: 'NoneType' object has no attribute 'author'`.

- [ ] **Step 3: Apply Fixes 1 and 2**

Change the membership tests to use `target_ap_id`:

```python
if '/post/' in target_ap_id:
    post = Post.get_by_ap_id(target_ap_id)
elif '/comment/' in target_ap_id:
    post_reply = PostReply.get_by_ap_id(target_ap_id)
else:
    post = Post.get_by_ap_id(target_ap_id)
    if post is None:
        post_reply = PostReply.get_by_ap_id(target_ap_id)
```

and, in the `if post_reply:` branch's modlog call, replace `target_user=post.author` with `target_user=post_reply.author` and `community=post.community` with `community=post_reply.community`.

- [ ] **Step 4: Run and watch them pass**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_moderation.py -q`
Expected: PASS

- [ ] **Step 5: Commit Fixes 1 and 2 together**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_undo_moderation.py
git commit -m "fix: unlock the right object, and read the reply's own author

Undo/Lock tested '/post/' and '/comment/' membership against
core_activity['object'], which is the inner Lock activity dict -- so
both branches were dead and every unlock fell to the else. The operand
is now target_ap_id, the string assigned immediately above.

That makes the '/comment/' branch live, which makes reachable a second
defect on the same branch: add_to_modlog read target_user=post.author
and community=post.community where post is always None. This is the
exact twin of D97, fixed in b79f43f9 in the Lock arm's own comment
branch. Both fixes land together because separating them would leave
the arm worse than before."
```

- [ ] **Step 6: Invert the test for Fix 3**

Replace `test_a_successful_post_unlock_also_logs_a_contradictory_failure` with:

```python
def test_a_successful_post_unlock_logs_success_and_nothing_else(app, db_session, monkeypatch):
    """FIX 3. The failure log now fires only when NEITHER a post nor a reply
    was found, so a successful post unlock records exactly one row.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()
    record_moderation(monkeypatch, 'add_to_modlog')
    post_id = post.id

    dispatch(undo_lock_activity(mod, post.ap_id))

    db.session.expire_all()
    assert db.session.get(type(post), post_id).comments_enabled is True

    logs = ActivityPubLog.query.all()
    assert len(logs) == 1
    assert logs[0].result == 'success'


def test_an_unlock_of_something_that_exists_nowhere_logs_not_found(app, db_session, monkeypatch):
    """The failure log's remaining reason to exist: neither a post nor a reply
    matched. Paired with the test above so the guard cannot be dropped in
    either direction.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, mod, community, author, post = _seed_lockable_post()

    dispatch(undo_lock_activity(mod, 'https://peer.example/post/404'))

    logs = ActivityPubLog.query.all()
    assert len(logs) == 1
    assert logs[0].result == 'failure'
    assert logs[0].exception_message == 'Unlock: post not found'
```

- [ ] **Step 7: Run and watch the first fail**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_moderation.py -q`
Expected: `test_a_successful_post_unlock_logs_success_and_nothing_else` FAILS with 2 logs.

- [ ] **Step 8: Apply Fix 3**

Change the trailing `else:` so the not-found log depends on neither object having been found:

```python
if not post and not post_reply:
    log_incoming_ap(id, APLOG_LOCK, APLOG_FAILURE, saved_json, 'Unlock: post not found')
```

placed after the `if post:` and `if post_reply:` blocks, replacing the misbound `else`.

- [ ] **Step 9: Run and watch both pass**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_moderation.py -q`
Expected: PASS

- [ ] **Step 10: Commit Fix 3**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_undo_moderation.py
git commit -m "fix: stop a successful unlock also logging 'post not found'

The trailing else bound to `if post_reply:` rather than to the pair, so
every successful POST unlock logged APLOG_SUCCESS and then immediately
logged APLOG_FAILURE 'Unlock: post not found' for the same activity.
The failure now depends on neither object having been found."
```

---

### Task 10: Undo/Block — both unban paths

**Files:**
- Modify: `tests/test_inbox_dispatch_undo_moderation.py`

Mirrors 5c's Block coverage closely; read `tests/test_inbox_dispatch_block.py` for the seeding helpers' shape before writing. `unblocked_ap_id` is lowercased before lookup, and `target` is read from the **inner** object, not the outer activity.

- [ ] **Step 1: Write the failing tests**

```python
def undo_block_activity(actor, blocked_ap_id, target, **outer):
    """An Undo wrapping a Block. `target` lives on the INNER object here,
    unlike the Block arm where it is on the outer activity.
    """
    return inbox_activity(actor, activity_type='Undo',
                          object={'type': 'Block', 'object': blocked_ap_id,
                                  'target': target}, **outer)


def _seed_site_unban(host='peer.example', grant_admin=True):
    """Unblocker and unblocked share ONE instance, and unblocked is remote --
    the only combination that reaches the ordinary site-unban write, since the
    is_local() and cross-instance checks both return before it.
    """
    instance = make_instance(host)
    unblocker = make_user(instance, 'admin')
    unblocker.ap_fetched_at = utcnow()
    if grant_admin:
        db.session.add(InstanceRole(instance_id=instance.id, user_id=unblocker.id, role='admin'))
    victim = make_user(instance, 'victim')
    victim.banned = True
    victim.banned_until = utcnow()
    db.session.commit()
    return instance, unblocker, victim


def test_site_unban_clears_banned_and_the_expiry(app, db_session, monkeypatch):
    """The ordinary site-unban write. Both columns are seeded to non-default
    values first, so clearing them is real evidence. This is the path whose
    correct use of `banned_until` was the decisive evidence in D91 that the
    Block arm's `ban_until` was a typo.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, victim = _seed_site_unban()
    victim_id = victim.id

    dispatch(undo_block_activity(unblocker, victim.ap_profile_id, f'https://{instance.domain}'))

    db.session.expire_all()
    fresh = db.session.get(User, victim_id)
    assert fresh.banned is False
    assert fresh.banned_until is None
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_site_unban_by_a_non_admin_is_refused(app, db_session, monkeypatch):
    """`is_instance_admin()` guard, checked before locality."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, victim = _seed_site_unban(grant_admin=False)
    victim_id = victim.id

    dispatch(undo_block_activity(unblocker, victim.ap_profile_id, f'https://{instance.domain}'))

    db.session.expire_all()
    assert db.session.get(User, victim_id).banned is True  # untouched
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Does not have permission'


def test_site_unban_of_an_unknown_user_is_ignored(app, db_session, monkeypatch):
    """No User row matches the lowercased ap_profile_id."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, victim = _seed_site_unban()

    dispatch(undo_block_activity(unblocker, 'https://peer.example/u/ghost',
                                 f'https://{instance.domain}'))

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Does not exist here'


def test_site_unban_of_a_local_user_delegates_to_unban_user(app, db_session, monkeypatch):
    """`unblocked.is_local()` -- delegates and returns before the direct write."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, _ = _seed_site_unban()
    victim = make_user(None, 'victim2', local=True)
    victim.ap_profile_id = f"{app.config['SERVER_URL']}/u/victim2".lower()
    victim.banned = True
    db.session.commit()
    victim_id = victim.id

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(unblocker, victim.ap_profile_id, f'https://{instance.domain}'))

    assert len(calls['unban_user']) == 1
    db.session.expire_all()
    assert db.session.get(User, victim_id).banned is True  # delegate was doubled
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Remote Admin in unbanning one of our users from their site'


def test_site_unban_of_a_user_on_a_third_instance_is_only_monitored(app, db_session, monkeypatch):
    """`unblocked.instance_id != unblocker.instance_id` -- no unban at all."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, unblocker, _ = _seed_site_unban()
    other = make_instance('third.example')
    victim = make_user(other, 'victim3')
    victim.banned = True
    db.session.commit()
    victim_id = victim.id

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(unblocker, victim.ap_profile_id, f'https://{instance.domain}'))

    assert calls['unban_user'] == []
    db.session.expire_all()
    assert db.session.get(User, victim_id).banned is True
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Remote Admin is unbanning a user of a different instance from their site'


def test_community_unban_delegates_to_unban_user(app, db_session, monkeypatch):
    """target.count('/') >= 4 selects the community branch. The unblocker is
    seeded as a moderator so the permission guard passes.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    mod = make_user(instance, 'mod')
    community = make_community(host='peer.example')
    make_community_member(mod, community, is_moderator=True)
    victim = make_user(instance, 'victim')
    victim.banned = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(mod, victim.ap_profile_id, community.ap_profile_id))

    assert len(calls['unban_user']) == 1
    args, kwargs = calls['unban_user'][0]
    assert args[2].id == community.id
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_community_unban_without_permission_is_refused(app, db_session, monkeypatch):
    """Neither moderator nor instance admin."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    outsider = make_user(instance, 'outsider')
    community = make_community(host='peer.example')
    victim = make_user(instance, 'victim')
    victim.banned = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'unban_user')

    dispatch(undo_block_activity(outsider, victim.ap_profile_id, community.ap_profile_id))

    assert calls['unban_user'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Does not have permission'


def test_community_unban_of_an_unfound_community_is_ignored(app, db_session, monkeypatch):
    """`find_actor_or_create_cached` doubled to None -- doubling is necessary,
    not convenient: an undoubled lookup attempts a real fetch, which
    block_outbound_http turns into a respx error, an INFRASTRUCTURE failure
    that would masquerade as a behavioural one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    mod = make_user(instance, 'mod')
    victim = make_user(instance, 'victim')
    victim.banned = True
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached',
                        lambda *args, **kwargs: None)

    dispatch(undo_block_activity(mod, victim.ap_profile_id,
                                 'https://peer.example/c/gone/moderators'))

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Blocked or unfound community'
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_moderation.py -q`
Expected: PASS

- [ ] **Step 3: Mutation-test the community permission guard**

Drop `not community.is_moderator(unblocker)`, confirm a named test fails; restore. Drop `not community.is_instance_admin(unblocker)`, confirm a named test fails; restore. If the second conjunct cannot be killed, add a test seeding an instance admin who is not a moderator — the guard's two halves must be killed **separately**.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_undo_moderation.py
git commit -m "test: cover Undo/Block's site and community unban paths"
```

---

### Task 11: Fix 4, the register, the README, and the floor

**Files:**
- Modify: `app/activitypub/routes.py` (ChooseAnswer only)
- Modify: `tests/test_inbox_dispatch_undo_content.py`
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`
- Modify: `coverage_floors.ini`

- [ ] **Step 1: Prove the dead branch, then remove it**

Add to `tests/test_inbox_dispatch_undo_content.py`:

```python
def test_a_string_inner_object_cannot_reach_choose_answer_at_all(app, db_session, monkeypatch):
    """FIX 4's justification. The arm selects ChooseAnswer by reading
    `core_activity['object']['type']`, so a STRING inner object raises
    TypeError before any sub-type is chosen -- which is why the
    `isinstance(core_activity['object'], str)` branch inside ChooseAnswer was
    unreachable. Same equivalent-mutant class as D95 and D96.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')

    activity = inbox_activity(author, activity_type='Undo',
                              object='https://peer.example/comment/1')

    with pytest.raises(TypeError):
        dispatch(activity)
```

Then replace the dead conditional in ChooseAnswer with the direct assignment:

```python
target_ap_id = core_activity['object']['object']
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_undo_content.py -q`
Expected: PASS. Add `import pytest` to the file if it is not already imported.

- [ ] **Step 3: Commit Fix 4**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_undo_content.py
git commit -m "fix: drop Undo/ChooseAnswer's unreachable string branch

Control only arrives in ChooseAnswer after
core_activity['object']['type'] == 'ChooseAnswer' succeeded, which a str
cannot satisfy -- subscripting a string by 'type' raises TypeError. The
isinstance(..., str) branch was dead. No behaviour changes."
```

- [ ] **Step 4: Register the findings**

Add a `## Sub-project 5d` section to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` in the same shape 5c used: a defects table numbered from **D104**, a section for the four authorised fixes, and an updated allocation ledger. Update the register's index note (the paragraph ending "Next free number") in this same change.

Every row needs: location, the defect, its impact, and how it was established — `measured` where a test proves it, `reading-level` where it was derived by reading. Do not overstate scope; 5c's D91 had to be corrected for exactly that.

- [ ] **Step 5: Record the harness facts**

Append to `tests/README.md`'s numbered harness-facts list, continuing its numbering: that `inbox_activity`'s `**fields` is applied last so `object=` overrides the default string object; that Undo dispatches on `core_activity['object']['type']` so a string inner object raises `TypeError` before any sub-type is selected; and that `add_to_modlog` must NOT be doubled when a test's purpose is to observe a dereference in its own arguments, since those are evaluated at the call site before the double is entered.

- [ ] **Step 6: Raise the floor**

The controller supplies the measured blended figure. Set `app/activitypub/routes.py` in `coverage_floors.ini` to that figure **rounded down**. Do not run the full suite in this task.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register 5d's findings and raise the routes.py floor"
```

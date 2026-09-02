# Sub-project 7: federated content ingestion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `process_new_content` — where every federated post and comment lands, 87 statements at 1.1% — to full statement coverage, and fix the four defects the spec names.

**Architecture:** Tests drive the function through `dispatch()` with a `Page`- or `Note`-typed object, in both the direct and Announce-wrapped shapes. `create_post` and `create_post_reply` are doubled in every test — their return value is the switch the function branches on. One new test file, no new factories. Defects are pinned first, then fixed test-first.

**Tech Stack:** pytest, pytest-timeout, respx, fakeredis, SQLAlchemy, Flask, podman-compose (`./run_tests.sh`).

**Spec:** `docs/superpowers/specs/2026-09-02-coverage-new-content-7-design.md`

## Global Constraints

- Defects found **in `process_new_content`** are fixed test-first, each in its own commit, separate from every test-only commit, each proved by a mutation that fails a named test. Anything outside the function is **registered, not fixed**.
- Findings are numbered from **D132**; the register's index note ("Next free number") is updated in the same change that takes them.
- The coverage floor in `coverage_floors.ini` rises to the measured blended figure **rounded down**.
- **Locate every code target by content, not by the line numbers in this plan.** They drift; every sub-project since 5c has found them stale.
- **`create_post` and `create_post_reply` are doubled in EVERY test.** They are large, write many rows, and are their own future slice. Their return value (`Post`/`PostReply` or `None`) is the branch switch, so each test chooses it deliberately.
- **No assertion may rest on a column's declared default.** Note `Post.edited_at` and `PostReply.edited_at` have **no** declared default (`app/models.py:1706`, `:2886`), so asserting `is None` on them is not vacuous — but a test of the *not*-a-lost-race side must seed a non-`None` value.
- Every guard is mutation-tested with **each conjunct dropped separately**, each killed by a distinct named test. The post half's permission check is a **three-way disjunction needing three kills**.
- A kill by `respx.models.AllMockedAssertionError` is an **infrastructure kill, not behavioural**.
- **Any docstring claim about another test must be verified true.** Sub-projects 5d, 5e and 6 each lost fix rounds to this.
- **Never double `find_actor_or_create_cached` unconditionally** — the preamble resolves the activity's own signed actor through it (`tests/README.md` fact 17).
- **One pytest session at a time** (fact 18). Implementers run only their own file; **the controller runs the full suite and supplies all coverage figures.**
- **Delete nothing** the task did not create. Agents in sub-project 6 destroyed an unrelated untracked file three times.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/test_inbox_dispatch_new_content.py` | **new** — all of `process_new_content` |
| `app/activitypub/routes.py` | defect fixes only, `process_new_content` only |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | findings from D132 |
| `tests/README.md`, `coverage_floors.ini` | harness facts, floor raise |

**No new factories.** `make_post(community, user, ap_id, title='a post', private=False, microblog=False)` and `make_post_reply(post, user, body='a reply')` both exist; `make_post_reply` does **not** set `ap_id`, so tests that need one must assign it.

**Shared helpers — reuse, do not rebuild:**

- `dispatch(activity, store_ap_json=True)` — `tests/test_inbox_dispatch_preamble.py`
- `record_moderation(monkeypatch, *names)` — `tests/test_inbox_dispatch_lock_delete.py`
- `inbox_activity(actor, *, activity_type='Like', object_uri=None, **fields)` — `tests/factories.py`; `**fields` applied last
- `seed_community_owner(domain) -> Instance` runs **before** `make_community(host=domain)`; `make_community_member(user, community, is_moderator=True)` grants moderator status

**Reaching the function.** The Create/Update arm dispatches to it when the inner object's type is in `['Page', 'Article', 'Link', 'Note', 'Question', 'Event']`, after `find_community` and `ensure_domains_match`. Tests double `find_community` to return the seeded community and `ensure_domains_match` to `True`.

**Delegate signatures** (all patchable on `app.activitypub.routes`):

```python
can_create_post(user, content: Community) -> bool                        # app/utils.py:2479
can_create_post_reply(user, content: Community) -> bool                  # app/utils.py:2531
create_post(store_ap_json, community, request_json, user, announce_id=None) -> Post|None   # util.py:2753
create_post_reply(store_ap_json, community, in_reply_to, request_json, user, announce_id=None)  # util.py:2572
update_post_from_activity(post, request_json)                            # util.py:3109
update_post_reply_from_activity(reply, request_json)                     # util.py:2987
proactively_delete_content(community, ap_id)                             # util.py:4675
find_microblogging_community()                                           # util.py:4607
announce_activity_to_followers(community, creator, activity, ...)        # DEFINED in routes.py:1946
```

---

### Task 1: the test file, and the preamble's two shapes

**Files:** Create `tests/test_inbox_dispatch_new_content.py`

**Interfaces — Produces:** `seed_content_pair(...)`, `content_object(...)`, `direct_activity(...)`, `announced_activity(...)`, used by every later task.

Covers the preamble: field extraction for a direct activity vs an Announce-wrapped one, and the `community is None` fallback to `find_microblogging_community()`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_inbox_dispatch_new_content.py"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, Post, PostReply, utcnow
from tests.factories import (inbox_activity, make_community, make_community_member,
                             make_instance, make_post, make_post_reply, make_site,
                             make_user, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def seed_content_pair(host='peer.example'):
    """A remote author and a community that `find_community` will be doubled to
    return. `seed_community_owner` runs FIRST because `make_community`
    hardcodes user_id=1/instance_id=1 against real foreign keys.

    `ap_fetched_at` is stamped so the preamble does not schedule an actor
    refresh, which would attempt a real fetch and surface as a respx error --
    an INFRASTRUCTURE failure that would masquerade as a behavioural one.
    """
    make_site()
    instance = seed_community_owner(host)
    community = make_community(host=host)
    author = make_user(instance, 'author')
    author.ap_fetched_at = utcnow()
    db.session.commit()
    return instance, community, author


def content_object(ap_id, *, object_type='Page', in_reply_to=None, **extra):
    """The inner content object. `in_reply_to` present selects the reply half
    of `process_new_content`; absent selects the post half.
    """
    obj = {'type': object_type, 'id': ap_id}
    if in_reply_to is not None:
        obj['inReplyTo'] = in_reply_to
    obj.update(extra)
    return obj


def direct_activity(author, obj, *, activity_type='Create'):
    """A Create/Update sent straight to the inbox: `announced` is False, and
    the function reads `inReplyTo`/`id` from request_json['object'].
    """
    return inbox_activity(author, activity_type=activity_type, object=obj)


def announced_activity(community, author, obj, *, activity_type='Create'):
    """The same content wrapped in an Announce from the community: `announced`
    is True, and the function reads from request_json['object']['object']
    instead, taking `announce_id` from the OUTER id.

    The outer actor must be the community -- the preamble resolves an
    Announce's actor with community_only=True.
    """
    inner = {'id': f'{author.ap_profile_id}/activities/inner',
             'type': activity_type,
             'actor': author.ap_profile_id,
             'object': obj}
    return inbox_activity(community, activity_type='Announce', object=inner)


def _double_the_gate(monkeypatch, community):
    """Doubles what stands between the arm's entry and process_new_content:
    community resolution and the domain check. Returns nothing -- callers that
    need call records double the delegates themselves.

    The ANNOUNCED tests deliberately do not call this: an Announce's actor is
    already the community, so the arm should resolve it without help. If an
    announced test cannot reach the function without this double, read how the
    arm resolves an Announce's community and say so here -- that is a fact
    later tasks need, not a reason to double blindly.
    """
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)


def test_a_direct_create_reads_the_object_and_passes_announced_false(app, db_session, monkeypatch):
    """The `not announced` half of the preamble. `create_post` is doubled and
    its arguments recorded: `announce_id` must be None for a direct activity,
    which is the observable difference from the announced shape below.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)

    calls = []
    monkeypatch.setattr(activitypub_routes, 'create_post',
                        lambda *args, **kwargs: calls.append((args, kwargs)) or None)
    record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs['announce_id'] is None
    assert args[2]['id'] == 'https://peer.example/post/1'


def test_an_announced_create_reads_the_nested_object_and_carries_an_announce_id(
        app, db_session, monkeypatch):
    """The `else` half of the preamble, reached only via an Announce. It reads
    the inner object one level deeper AND sets `announce_id` from the outer
    activity's id -- the only place that value comes from.

    Paired with the test above so neither half of the preamble can be dropped
    without a failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    community.ap_fetched_at = utcnow()
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)

    calls = []
    monkeypatch.setattr(activitypub_routes, 'create_post',
                        lambda *args, **kwargs: calls.append((args, kwargs)) or None)
    record_moderation(monkeypatch, 'proactively_delete_content')

    activity = announced_activity(community, author, content_object('https://peer.example/post/1'))
    dispatch(activity)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs['announce_id'] == activity['id']
    assert args[2]['id'] == 'https://peer.example/post/1'


def test_a_missing_community_falls_back_to_the_microblogging_community(app, db_session, monkeypatch):
    """`if community is None:` -- reached when `find_community` finds nothing,
    which the arm treats as a microblogging post. The fallback's return value
    is what every later dereference of `community` uses, so it is asserted by
    identity through `can_create_post`'s argument rather than merely by the
    delegate being called.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    microblog = make_community(name='microblog', host='peer.example')
    db.session.commit()

    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *a, **k: False)
    monkeypatch.setattr(activitypub_routes, 'find_microblogging_community', lambda: microblog)

    seen = {}

    def fake_can_create_post(user, content):
        seen['community_id'] = content.id
        return False

    monkeypatch.setattr(activitypub_routes, 'can_create_post', fake_can_create_post)
    record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert seen['community_id'] == microblog.id
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_inbox_dispatch_new_content.py -q`
Expected: PASS. If the announced activity does not reach the function, read the preamble's Announce handling and adjust — then say in `announced_activity`'s docstring what it actually requires.

- [ ] **Step 3: Mutation-test the preamble's branch**

Swap the `if not announced:` body with its `else` body (so both read the same nesting level) and confirm `test_an_announced_create_reads_the_nested_object_and_carries_an_announce_id` fails. Restore; verify `git diff app/activitypub/routes.py` is empty. Record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_new_content.py
git commit -m "test: cover process_new_content's preamble in both activity shapes"
```

---

### Task 2: the post half — an existing post

**Files:** Modify `tests/test_inbox_dispatch_new_content.py`

Three outcomes: an existing post with a `Create` (refused), with a permitted editor (updated + announced), with a denied editor.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_create_for_an_existing_post_is_refused_as_processed_after_update(
        app, db_session, monkeypatch):
    """`activity_json['type'] == 'Create'` for a post that already exists means
    an Update won an async race and this Create arrived late. Refused before
    any permission check, so the actor here is the post's own author -- proving
    the refusal is about ordering, not permission.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(direct_activity(author, content_object(post.ap_id)))

    assert calls['update_post_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Create processed after Update'


def test_an_update_by_the_posts_author_updates_and_announces(app, db_session, monkeypatch):
    """First disjunct of the permission check: `user.id == post.user_id`. The
    announce fires because the activity is not announced -- its own guard,
    asserted here and pinned from the other side by the announced test below.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object(post.ap_id), activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_announced_update_does_not_re_announce(app, db_session, monkeypatch):
    """`if not announced:` -- the other side. An Announce-wrapped Update still
    updates the post but must not be re-announced to followers, or the activity
    would loop back out to the instance that sent it.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    community.ap_fetched_at = utcnow()
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(announced_activity(community, author, content_object(post.ap_id),
                                activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    assert calls['announce_activity_to_followers'] == []


def test_an_update_by_an_unrelated_user_is_denied(app, db_session, monkeypatch):
    """All three disjuncts false: not the author, not a moderator, not an
    instance admin. This is the test the three permission mutations in Task 3
    are measured against.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    outsider = make_user(instance, 'outsider')
    outsider.ap_fetched_at = utcnow()
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(direct_activity(outsider, content_object(post.ap_id), activity_type='Update'))

    assert calls['update_post_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Edit attempt denied'
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the announce guard**

Remove `if not announced:` (de-indent its body) and confirm `test_an_announced_update_does_not_re_announce` fails. Restore; verify the production diff is empty; record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_new_content.py
git commit -m "test: cover the post half's existing-post outcomes"
```

---

### Task 3: the post half's three-way permission disjunction

**Files:** Modify `tests/test_inbox_dispatch_new_content.py`

`user.id == post.user_id or post.community.is_moderator(user) or post.community.is_instance_admin(user)` — **three disjuncts, three distinct kills.** Task 2 supplied the author case; this task supplies the other two.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_update_by_a_community_moderator_is_permitted(app, db_session, monkeypatch):
    """Second disjunct. The editor is NOT the author, so the first disjunct is
    false and this test isolates `post.community.is_moderator(user)`.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    mod = make_user(instance, 'mod')
    mod.ap_fetched_at = utcnow()
    make_community_member(mod, community, is_moderator=True)
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(mod, content_object(post.ap_id), activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_update_by_an_instance_admin_is_permitted(app, db_session, monkeypatch):
    """Third disjunct: `post.community.is_instance_admin(user)`. The editor is
    neither the author nor a moderator, so this test is the only one that can
    kill that disjunct.

    `Community.is_instance_admin(user)` checks an InstanceRole against the
    COMMUNITY's instance -- a different method from `User.is_instance_admin()`,
    which takes no arguments and checks the user's own. Read both before
    seeding; sub-project 5c documented the pair being easy to conflate.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    admin = make_user(instance, 'admin')
    admin.ap_fetched_at = utcnow()
    from app.models import InstanceRole
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=admin.id, role='admin'))
    post = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(direct_activity(admin, content_object(post.ap_id), activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
```

- [ ] **Step 2: Run the file**

Expected: PASS. If the instance-admin test fails, read `Community.is_instance_admin` and seed what it actually reads — then say in the docstring what that is. **Do not change production code to make it pass.**

- [ ] **Step 3: Mutation-test all three disjuncts separately**

Three edits, restoring after each: drop `user.id == post.user_id or`; drop `post.community.is_moderator(user) or`; drop `or post.community.is_instance_admin(user)`. Each must be killed by a distinct named test. Verify the production diff is empty before committing. **Record all three separately.** If any cannot be killed, say so plainly — that is a finding.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_new_content.py
git commit -m "test: pin all three disjuncts of the post half's permission check"
```

---

### Task 4: the post half — creating a new post

**Files:** Modify `tests/test_inbox_dispatch_new_content.py`

`can_create_post` true → `create_post` returns a post → the Update-lost-the-race check → SUCCESS + announce.

- [ ] **Step 1: Write the failing tests**

```python
def _permit_and_return(monkeypatch, post_or_none):
    """Double `can_create_post` to True and `create_post` to return the given
    object. `create_post` is doubled in every test in this file: it is large,
    writes many rows, and is its own future slice -- and its return value is
    exactly the switch this function branches on.
    """
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)
    monkeypatch.setattr(activitypub_routes, 'create_post',
                        lambda *args, **kwargs: post_or_none)


def _seed_created_post(community, author):
    """The Post that `create_post` is doubled to return.

    Its ap_id MUST DIFFER from the one the test dispatches. `process_new_content`
    looks up an existing post by the dispatched ap_id BEFORE it reaches the
    creation path -- seeding the returned post under the dispatched ap_id would
    make the function take the existing-post branch instead, and the test would
    silently measure Task 2's path rather than this one.
    """
    return make_post(community, author, 'https://peer.example/post/created')


def test_a_create_that_succeeds_logs_success_and_announces(app, db_session, monkeypatch):
    """The ordinary new-post path. `edited_at` is left None (the column has no
    declared default), so the lost-race branch below is the one NOT taken here
    -- proved by `update_post_from_activity` never being called.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    created = _seed_created_post(community, author)
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, created)
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object('https://peer.example/post/new')))

    assert calls['update_post_from_activity'] == []
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_update_that_lost_a_race_to_a_create_is_applied_afterwards(app, db_session, monkeypatch):
    """`activity_json['type'] == 'Update' and post.edited_at is None` -- an
    Update arrived, found no post, created one, and must then apply itself.
    Both conjuncts are true here.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    created = _seed_created_post(community, author)
    created.edited_at = None
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, created)
    calls = record_moderation(monkeypatch, 'update_post_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object('https://peer.example/post/new'),
                             activity_type='Update'))

    assert len(calls['update_post_from_activity']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_update_on_an_already_edited_post_is_not_re_applied(app, db_session, monkeypatch):
    """The second conjunct: `post.edited_at is None` is FALSE, so the freshly
    created post is left alone. `edited_at` is seeded to an explicit timestamp
    -- the column has no declared default, so the value is this test's own
    choice and the assertion is not vacuous.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    created = _seed_created_post(community, author)
    created.edited_at = utcnow()
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, created)
    calls = record_moderation(monkeypatch, 'update_post_from_activity')

    dispatch(direct_activity(author, content_object('https://peer.example/post/new'),
                             activity_type='Update'))

    assert calls['update_post_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test both conjuncts of the lost-race check**

Drop `activity_json['type'] == 'Update' and` and confirm a named test fails; restore. Drop `and post.edited_at is None` and confirm a named test fails; restore. Verify the production diff is empty; record both.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_new_content.py
git commit -m "test: cover the post half's creation path and its lost-race check"
```

---

### Task 5: the post half — refusals, and the silent fallthrough

**Files:** Modify `tests/test_inbox_dispatch_new_content.py`

Three outcomes: `create_post` returns `None` (proactive delete, **no log, no return** — pin this), a `TypeError` from the delegate, and `can_create_post` false.

**One of these pins a defect. Do not fix it here** — Task 9 does.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_refused_post_is_deleted_remotely_and_logs_nothing(app, db_session, monkeypatch):
    """PINS a defect. `create_post` returning None means the post was not
    allowed, so a Delete is sent back to the remote instance -- but the branch
    then neither logs nor returns. Control leaves the `try` without the
    `except` firing, leaves `if can_create_post(...)`, leaves the post half,
    and falls off the end of the function.

    Asserted with LOG_ACTIVITYPUB_TO_DB explicitly True, so the zero is real
    silence rather than logging being switched off. An operator cannot tell a
    refused-and-deleted post from one that was never received.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, None)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert len(calls['proactively_delete_content']) == 1
    args, kwargs = calls['proactively_delete_content'][0]
    assert args[1] == 'https://peer.example/post/1'
    assert ActivityPubLog.query.count() == 0


def test_a_refused_post_in_a_remote_community_is_not_deleted(app, db_session, monkeypatch):
    """`if community.is_local():` -- the other side. A remote community's own
    instance is responsible for its content, so no Delete is sent.

    `make_community` never sets `ap_id`, so every factory community is
    is_local() == True regardless of host (tests/README.md fact 20); the
    community is made genuinely remote here by setting `ap_id` explicitly.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    community.ap_id = 'https://peer.example/c/microblogs'
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return(monkeypatch, None)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert calls['proactively_delete_content'] == []


def test_a_type_error_from_create_post_is_logged_and_returned(app, db_session, monkeypatch):
    """The `except TypeError:` fallback. The delegate is doubled to raise, which
    is the only way to reach it -- nothing in this function raises TypeError
    itself.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: True)

    def boom(*args, **kwargs):
        raise TypeError('malformed')

    monkeypatch.setattr(activitypub_routes, 'create_post', boom)

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'TypeError. See log file.'


def test_a_user_who_cannot_post_is_refused_and_their_content_deleted(app, db_session, monkeypatch):
    """`can_create_post` false. Unlike the refused-by-the-delegate branch above,
    this one DOES log -- which is the asymmetry the register records.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post', lambda user, content: False)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/post/1')))

    assert len(calls['proactively_delete_content']) == 1
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'User cannot create post in Community'
```

- [ ] **Step 2: Run the file** — Expected: PASS. Every assertion describes current behaviour; a failure means the defect is not as described, which you report rather than adjusting.

- [ ] **Step 3: Commit**

```bash
git add tests/test_inbox_dispatch_new_content.py
git commit -m "test: cover the post half's refusals and pin its silent fallthrough"
```

---

### Task 6: the reply half — an existing reply

**Files:** Modify `tests/test_inbox_dispatch_new_content.py`

The mirror of Task 2, plus the reply half's extra inner `can_create_post_reply` check — **which pins the second defect.**

- [ ] **Step 1: Write the failing tests**

```python
def _seed_reply(community, author, ap_id='https://peer.example/comment/1'):
    """A reply with an explicit ap_id -- `make_post_reply` does not set one, and
    `process_new_content` resolves the reply by exactly that value.
    """
    parent = make_post(community, author, 'https://peer.example/post/1')
    reply = make_post_reply(parent, author)
    reply.ap_id = ap_id
    db.session.commit()
    return parent, reply


def test_a_create_for_an_existing_reply_is_refused_as_processed_after_update(
        app, db_session, monkeypatch):
    """The reply half's mirror of the post half's ordering refusal."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(author, content_object(reply.ap_id, in_reply_to=parent.ap_id)))

    assert calls['update_post_reply_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Create processed after Update'


def test_an_update_by_the_replys_author_updates_and_announces(app, db_session, monkeypatch):
    """The permitted path, with `can_create_post_reply` true."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object(reply.ap_id, in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert len(calls['update_post_reply_from_activity']) == 1
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_permitted_editor_who_cannot_reply_is_dropped_silently(app, db_session, monkeypatch):
    """PINS a defect. The outer permission check passes but
    `can_create_post_reply` is false, so the bare `return` fires with NO log on
    any path -- the update does not happen and nothing records why.

    The post half has no equivalent inner check at all, which is what makes
    this an asymmetry rather than a deliberate design.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: False)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(author, content_object(reply.ap_id, in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert calls['update_post_reply_from_activity'] == []
    assert ActivityPubLog.query.count() == 0


def test_an_instance_admin_cannot_edit_a_reply(app, db_session, monkeypatch):
    """PINS the headline defect. The post half permits an instance admin via a
    third disjunct; the reply half's check is
    `user.id == reply.user_id or reply.community.is_moderator(user)` -- the
    third disjunct is absent, so the same admin who may edit a post is refused
    on a reply.

    Seeded identically to Task 3's post-side admin test, so the difference
    observed is the code's, not the fixture's.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    admin = make_user(instance, 'admin')
    admin.ap_fetched_at = utcnow()
    from app.models import InstanceRole
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=admin.id, role='admin'))
    parent, reply = _seed_reply(community, author)
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(admin, content_object(reply.ap_id, in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert calls['update_post_reply_from_activity'] == []
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Edit attempt denied'
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the reply permission check's two disjuncts**

Drop `user.id == reply.user_id or` and confirm a named test fails; restore. Drop `or reply.community.is_moderator(user)` and confirm a named test fails; restore. If the second cannot be killed, **add a moderator test** — the guard's halves must die separately. Record both.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_new_content.py
git commit -m "test: cover the reply half's existing-reply outcomes and pin two defects"
```

---

### Task 7: the reply half — creating a new reply, and its refusals

**Files:** Modify `tests/test_inbox_dispatch_new_content.py`

The mirror of Tasks 4 and 5, using `create_post_reply` and `can_create_post_reply`.

- [ ] **Step 1: Write the failing tests**

```python
def _permit_and_return_reply(monkeypatch, reply_or_none):
    """`create_post_reply` is doubled in every test for the same reason
    `create_post` is: it is large, writes many rows, and is its own future
    slice, and its return value is the switch this half branches on.
    """
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)
    monkeypatch.setattr(activitypub_routes, 'create_post_reply',
                        lambda *args, **kwargs: reply_or_none)


def test_a_new_reply_that_succeeds_logs_success_and_announces(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author, ap_id='https://peer.example/comment/existing')
    _double_the_gate(monkeypatch, community)
    _permit_and_return_reply(monkeypatch, reply)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity',
                              'announce_activity_to_followers')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id)))

    assert calls['update_post_reply_from_activity'] == []
    assert len(calls['announce_activity_to_followers']) == 1
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_reply_update_that_lost_a_race_is_applied_afterwards(app, db_session, monkeypatch):
    """`activity_json['type'] == 'Update' and reply.edited_at is None`, both
    conjuncts true. `edited_at` has no declared default on PostReply either.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author, ap_id='https://peer.example/comment/existing')
    reply.edited_at = None
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return_reply(monkeypatch, reply)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert len(calls['update_post_reply_from_activity']) == 1


def test_a_reply_update_on_an_already_edited_reply_is_not_re_applied(app, db_session, monkeypatch):
    """The second conjunct false: `edited_at` seeded to an explicit timestamp."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent, reply = _seed_reply(community, author, ap_id='https://peer.example/comment/existing')
    reply.edited_at = utcnow()
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return_reply(monkeypatch, reply)
    calls = record_moderation(monkeypatch, 'update_post_reply_from_activity')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id),
                             activity_type='Update'))

    assert calls['update_post_reply_from_activity'] == []


def test_a_refused_reply_is_deleted_remotely_and_logs_nothing(app, db_session, monkeypatch):
    """The reply half's mirror of the post half's refusal. This one DOES return
    explicitly, unlike its post-half twin -- but it still logs nothing, which
    is the shared half of that defect.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    _permit_and_return_reply(monkeypatch, None)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id)))

    assert len(calls['proactively_delete_content']) == 1
    assert ActivityPubLog.query.count() == 0


def test_a_type_error_from_create_post_reply_is_logged(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: True)

    def boom(*args, **kwargs):
        raise TypeError('malformed')

    monkeypatch.setattr(activitypub_routes, 'create_post_reply', boom)

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id)))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'TypeError. See log file.'


def test_a_user_who_cannot_reply_is_refused_and_their_content_deleted(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    parent = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    _double_the_gate(monkeypatch, community)
    monkeypatch.setattr(activitypub_routes, 'can_create_post_reply', lambda user, content: False)
    calls = record_moderation(monkeypatch, 'proactively_delete_content')

    dispatch(direct_activity(author, content_object('https://peer.example/comment/new',
                                                    in_reply_to=parent.ap_id)))

    assert len(calls['proactively_delete_content']) == 1
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'User cannot create reply in Community'
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the reply half's lost-race conjuncts**

Drop each conjunct of `activity_json['type'] == 'Update' and reply.edited_at is None` in turn, confirming a distinct named test fails each time. Restore after each; verify the production diff is empty; record both.

- [ ] **Step 4: Commit**

```bash
git add tests/test_inbox_dispatch_new_content.py
git commit -m "test: cover the reply half's creation path and refusals"
```

---

### Task 8: pin the in-place mutation of the caller's activity

**Files:** Modify `tests/test_inbox_dispatch_new_content.py`

The fourth defect, and the only one not yet pinned. **Assert current behaviour; do not fix.**

- [ ] **Step 1: Write the failing test**

```python
def test_the_id_truncation_mutates_the_callers_activity(app, db_session, monkeypatch):
    """PINS a defect. `activity_json['id'] = shorten_string(activity_json['id'], 100)`
    writes back into the dict the caller owns. For an ANNOUNCED activity
    `activity_json` IS `request_json['object']`, so the truncation is visible
    in the activity object this test constructed -- and `request_json` is then
    handed to `announce_activity_to_followers`, so the truncated id propagates
    to followers.

    The comment above that line claims the id is "not referred to again, so it
    shouldn't matter if they're truncated". This test is the counter-example:
    the object asserted below is the very dict the test passed in.

    The inner id is deliberately longer than 100 characters so truncation is
    observable; a short id would leave the mutation invisible.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community, author = seed_content_pair()
    community.ap_fetched_at = utcnow()
    db.session.commit()
    _permit_and_return(monkeypatch, None)
    record_moderation(monkeypatch, 'proactively_delete_content')

    long_id = f'{author.ap_profile_id}/activities/' + ('x' * 150)
    activity = announced_activity(community, author, content_object('https://peer.example/post/1'))
    activity['object']['id'] = long_id
    original_length = len(activity['object']['id'])

    dispatch(activity)

    assert original_length > 100
    assert len(activity['object']['id']) == 100
    assert activity['object']['id'] == long_id[:100]
```

- [ ] **Step 2: Run the file**

Expected: PASS. If the mutation is not observable — for instance because `dispatch` deep-copies — report that: the defect would then be narrower than the spec claims, and Task 9's fix would be unnecessary.

- [ ] **Step 3: Commit**

```bash
git add tests/test_inbox_dispatch_new_content.py
git commit -m "test: pin the in-place id truncation of the caller's activity"
```

---

### Task 9: fix the defects

**Files:** Modify `app/activitypub/routes.py` (`process_new_content` only); modify `tests/test_inbox_dispatch_new_content.py`

Invert the pinning tests, watch them fail, then fix. **Report the actual failure output** — that step is the proof. **Four fixes, four separate commits.**

**Fix A — the instance-admin asymmetry.** Add the missing third disjunct to the reply half's permission check so it matches the post half's. Invert `test_an_instance_admin_cannot_edit_a_reply`.

**Fix B — the silent reply no-op.** A permitted editor who fails `can_create_post_reply` must be logged. Choose the message and level yourself from the arm's idiom and justify them in the report — the function's existing refusals use `log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, '<reason>')`.

**Fix C — the post half's silent fallthrough.** The refused-post branch must log and return, like every other terminal path. Decide whether the reply half's mirror (which returns but does not log) should gain the same log, and say why — the two should end consistent.

**Fix D — the in-place mutation.** Truncate without writing back into the caller's dict. Decide the shape yourself: the truncated value is used for `create_post`/`create_post_reply`'s `request_json` and for `announce_id`. Say in the report what you changed and what now receives the untruncated id. **If your fix would change what is stored or federated, say so and prefer the smaller change** — the defect registered is the mutation of a caller's structure, not the truncation itself.

**Fix A — the instance-admin asymmetry**

- [ ] **Step 1: Invert the pinning test.** Rename `test_an_instance_admin_cannot_edit_a_reply` to `test_an_instance_admin_can_edit_a_reply`, and change its assertions to `len(calls['update_post_reply_from_activity']) == 1` and `log.result == 'success'`. Update its docstring: it no longer pins a defect, it proves the halves now agree.
- [ ] **Step 2: Run it and watch it fail.** `./run_tests.sh tests/test_inbox_dispatch_new_content.py -q`. Quote the failure in your report — that output is the proof the test discriminates.
- [ ] **Step 3: Apply the fix.** Add `or reply.community.is_instance_admin(user)` to the reply half's permission check, so it reads as the post half's does. Run again; expect PASS.
- [ ] **Step 4: Mutation-test it.** Remove the disjunct you just added, confirm `test_an_instance_admin_can_edit_a_reply` fails, restore. Verify `git diff app/activitypub/routes.py` shows only the fix.
- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_new_content.py
git commit -m "fix: let an instance admin edit a federated reply, as they can a post"
```

**Fix B — the silent reply no-op**

- [ ] **Step 6: Invert the pinning test.** Rename `test_a_permitted_editor_who_cannot_reply_is_dropped_silently` to `test_a_permitted_editor_who_cannot_reply_is_logged`, keep `calls['update_post_reply_from_activity'] == []`, and replace the `ActivityPubLog.query.count() == 0` assertion with one on the log's `result` and `exception_message`.
- [ ] **Step 7: Run it and watch it fail.** Quote the failure.
- [ ] **Step 8: Apply the fix.** Add a `log_incoming_ap` call on the `can_create_post_reply`-false path before the bare `return`. **Choose the message and level from the arm's idiom** — its existing refusals use `log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, '<reason>')` — and justify your choice in the report. Run; expect PASS.
- [ ] **Step 9: Mutation-test it.** Delete the log call, confirm the named test fails, restore.
- [ ] **Step 10: Commit**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_new_content.py
git commit -m "fix: log a reply edit refused by can_create_post_reply"
```

**Fix C — the post half's silent fallthrough**

- [ ] **Step 11: Invert the pinning test.** Rename `test_a_refused_post_is_deleted_remotely_and_logs_nothing` to `test_a_refused_post_is_deleted_remotely_and_logged`, keep the `proactively_delete_content` assertions, and replace `ActivityPubLog.query.count() == 0` with assertions on the log's `result` and `exception_message`.
- [ ] **Step 12: Run it and watch it fail.** Quote the failure.
- [ ] **Step 13: Apply the fix.** Add a `log_incoming_ap(...)` and a `return` to the post half's refused-creation branch, so it terminates like every other path in the function. **Decide whether the reply half's mirror — `test_a_refused_reply_is_deleted_remotely_and_logs_nothing`, which returns but does not log — should gain the same log, and say why in your report.** If you add it, invert that test the same way in this commit; if you do not, say what makes the two cases different. The two should end consistent.
- [ ] **Step 14: Mutation-test it.** Delete the `return` alone and confirm nothing fails (that is expected — the fallthrough reached the end of the function anyway, and this is worth reporting); then delete the log call and confirm the named test fails. Restore both.
- [ ] **Step 15: Commit**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_new_content.py
git commit -m "fix: log and return when a federated post's creation is refused"
```

**Fix D — the in-place mutation**

- [ ] **Step 16: Invert the pinning test.** Rename `test_the_id_truncation_mutates_the_callers_activity` to `test_the_id_truncation_leaves_the_callers_activity_untouched`, and change the assertions to `len(activity['object']['id']) == original_length` and `activity['object']['id'] == long_id`.
- [ ] **Step 17: Run it and watch it fail.** Quote the failure.
- [ ] **Step 18: Apply the fix.** Truncate into a local name instead of writing back into `activity_json`. **Decide the shape yourself** — the truncated value feeds `create_post`/`create_post_reply`'s `request_json` and `announce_id`. Say in your report exactly what you changed and what now receives the untruncated id. **If your fix would change what is stored or federated, say so and prefer the smaller change:** the registered defect is the mutation of a caller's structure, not the truncation itself. Also correct the comment above the line, which currently denies that the id is referred to again.
- [ ] **Step 19: Mutation-test it.** Restore the in-place assignment, confirm the named test fails, restore the fix.
- [ ] **Step 20: Commit**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_new_content.py
git commit -m "fix: stop truncating the caller's activity id in place"
```

**Do not fix** `find_microblogging_community()` returning `None` and the subsequent unguarded dereferences of `community`. Task 10 registers it — whether that return is reachable is a question the tests should answer before any code change.

---

### Task 10: the register, the README, and the floor

**Files:** Modify the findings register, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Register the findings**

Add a `## Sub-project 7` section to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, in the shape sub-project 6's section uses (**read it first**). Number from **D132**; update the index note's "Next free number" in the same change.

**Number by distinct defect, not by commit** — the register's convention, upheld by rulings in 5c, 5d and 6 (D98/D99, D101/D102, D105/D106, D121/D122).

Source from the task reports. Every row: location, defect, impact, and how established — `measured` or `reading-level`. **Verify every line number against the file before writing it**; citation errors are this register's recurring defect.

Register at minimum:

- Task 9's four fixes, with commits.
- **`find_microblogging_community()` can return `None`**, after which `community` is dereferenced unguarded in both halves. Not fixed — say what the tests established about reachability, or that they established nothing.
- Whether the reply half's refused-reply branch was left without a log (see Fix C's decision), if it was.

- [ ] **Step 2: Record the harness facts**

Append to `tests/README.md`'s numbered list, continuing its numbering, whatever this sub-project established that a later one would otherwise rediscover — at minimum that `Post.edited_at` and `PostReply.edited_at` have **no declared default**, so asserting `is None` on them is not vacuous, and that reaching `process_new_content` requires doubling `find_community` and `ensure_domains_match`.

- [ ] **Step 3: Raise the floor**

The controller supplies the measured blended figure. Set `app/activitypub/routes.py` in `coverage_floors.ini` to that figure **rounded down**. **Do not run the full suite.**

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 7's findings and raise the routes.py floor"
```

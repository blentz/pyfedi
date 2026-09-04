# Sub-project 13: refresh-profile trio coverage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `refresh_user_profile_task`, `refresh_community_profile_task` and `refresh_feed_profile_task` to full statement coverage, fix the two crash paths two of the three share, and register the rest.

**Architecture:** One new test file calling the three Celery tasks **directly as plain functions**, with `http_mock` serving the actor documents. Every test asserts on persisted row state. The three are compared against each other by design — the asymmetries are the findings.

**Tech Stack:** pytest, respx (via `http_mock`), SQLAlchemy, `tests/factories.py`.

**Spec:** `docs/superpowers/specs/2026-09-03-coverage-refresh-profiles-13-design.md`

## Global Constraints

- Defects found **inside these three functions** are fixed test-first, each in its own commit, each proved by a mutation that fails a named test. Anything outside them is **registered, not fixed**.
- Findings are numbered from **D213**, and **both live "Next free number" notes** are updated in the same change. The historical notes are frozen records — leave them alone.
- The floor for `app/activitypub/util.py` rises to the measured blended figure rounded down. It currently reads **55**; measured is **55.1080%**.
- **Locate every code target by content, not by the line numbers in this plan.** Task 10's fixes will shift lines.
- **Every test asserts on persisted row state**, not merely on the absence of an exception. These tasks commit the profile before doing anything else, so "nothing raised" cannot distinguish a fix from a guard that abandoned the document.
- **No vacuous assertions.** `Instance.dormant` and `Instance.gone_forever` default to `False` so `online()` is true for a factory-built instance; `Instance.failures` defaults to 0.
- The full suite must pass. **Only the controller runs it, one session at a time**, and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository root is not the campaign's.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_ap_refresh_profiles.py` | **Create.** All tests for the three tasks' fetch-and-apply paths. |
| `app/activitypub/util.py` | **Modify, Task 10 only.** The two crash fixes. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Task 11 only.** |
| `tests/README.md` | **Modify, Task 11 only.** |
| `coverage_floors.ini` | **Modify, Task 11 only.** |

`tests/test_ap_refresh_community_profile.py` is **not modified**. It covers the legacy flair loop, a different part of one of the three.

## Helpers each task inherits

SDD implementers see only their own brief, so this table is repeated into every brief. Task 1 creates these; Tasks 2-9 import and use them and **must not reimplement them**.

| Name | Where | Signature / behaviour |
|---|---|---|
| `PEER` | this file | `'peer.example'` — **must not end in `.local`**, see fact 3 |
| `_remote_user(name='wakko')` | this file | a remote `User` on an online instance, ready to refresh |
| `_remote_community(name='memes')` | this file | a remote `Community`, `ap_followers_url` cleared |
| `_remote_feed(name='news')` | this file | a remote `Feed` on an online instance, not local |
| `_person_document(name='wakko', fields=None)` | this file | the peer's `Person` document |
| `_group_document(name='memes', fields=None)` | this file | the peer's `Group` document |
| `_feed_document(name='news', fields=None)` | this file | the peer's `Feed` document |
| `_serve(http_mock, url, document=None, status=200, text=None)` | this file | register one respx route |
| `make_user(instance, name, local=False, with_keys=False)` | `tests/factories.py` | |
| `make_community(name='microblogs', host='test.piefed.local')` | `tests/factories.py` | **sets `ap_followers_url`** — clear it |
| `make_local_feed(name='localfeed', public=False)` | `tests/factories.py` | local; for a remote feed use `make_feed` |
| `make_instance(domain, software='mastodon')` | `tests/factories.py` | |
| `seed_community_owner(domain='peer.example')` | `tests/factories.py` | creates Instance id 1 **and** User id 1 |
| `http_mock` | `tests/conftest.py` fixture | respx router, **`assert_all_called=True`** |
| `no_real_sleeping` | `tests/conftest.py` fixture | **required by every retry test** |

## Facts every task needs

1. **These are Celery tasks tested as plain functions.** Every caller invokes them inline under `current_app.debug`, and the `app` fixture puts celery in eager mode. Calling the undecorated function is the same code path a worker runs, with no mock anywhere. `tests/test_ap_refresh_community_profile.py` established this.
2. **`http_mock` uses `assert_all_called=True`.** A route you register and never exercise **fails the test**. Register only what the test actually provokes.
3. **`get_request()` rejects domains ending in `.local`** via `is_invalid_get_request_uri()`, before respx ever sees the request. Use `peer.example`. A `.local` fixture fails for a reason that has nothing to do with the code under test.
4. **`block_outbound_http` (`tests/conftest.py`) raises on any unmocked request.** That is a feature: a test that provokes an unexpected fetch fails loudly. Do not work around it.
5. **All three tasks `time.sleep(randint(3, 10))` between the first failure and the retry.** Any test reaching that path **must** request `no_real_sleeping` or it sleeps up to ten real seconds.
6. **Only `refresh_community_profile_task` takes `activity_json`.** Passing it skips the fetch entirely. `refresh_user_profile_task(user_id)` and `refresh_feed_profile_task(feed_id)` take an id only and **always fetch**.
7. **The three collections are opt-in.** Moderators, followers and featured are fetched only when the corresponding URL is set on the row. `make_community` sets `ap_followers_url`; clear it unless the test means to exercise that fetch.
8. **`get_task_session()` is a plain `Session` with autoflush at SQLAlchemy's default `True`** — unlike `db.session`, which the app factory configures `session_options={"autoflush": False}`. Say in any docstring whether an assertion depends on that.
9. **`Instance.online()` is `not (self.dormant or self.gone_forever)`** (`app/models.py`). Both default `False`, so a factory-built instance is online; a test needing an offline one sets one flag explicitly.
10. **No factory produces a row with `instance_id` NULL** — `make_user`, `make_community` and the feed factories all set it. A test needing that state sets the column explicitly after construction.

---

### Task 1: Scaffold, helpers, and `refresh_user_profile_task`'s happy path

**Files:**
- Create: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PEER`, `_remote_user`, `_remote_community`, `_remote_feed`, `_person_document`, `_group_document`, `_feed_document`, `_serve`. Every later task uses these.

- [ ] **Step 1: Write the file header and helpers**

```python
"""tests/test_ap_refresh_profiles.py

The three refresh-profile tasks, driven directly.

They are celery tasks, but every caller invokes them inline under
`current_app.debug` and the `app` fixture puts celery in eager mode, so
calling the undecorated function is the same code path a worker runs, with no
mock anywhere. `tests/test_ap_refresh_community_profile.py` established this
for the community task and covers its legacy flair loop; this file covers the
actor re-fetch that file scopes out, for all three tasks.

`PEER` must not end in `.local`: `get_request()` rejects those via
`is_invalid_get_request_uri()` before respx ever sees the request, so a
`.local` fixture fails for a reason unrelated to the code under test.
"""
import httpx
import pytest

from app import db
from app.activitypub.util import (refresh_community_profile_task,
                                  refresh_feed_profile_task,
                                  refresh_user_profile_task)
from app.models import Community, Feed, Instance, User
from tests.factories import (make_community, make_feed, make_instance, make_user,
                             seed_community_owner)

PEER = 'peer.example'


def _remote_user(name='wakko'):
    """A remote user on an online instance, ready to refresh.

    `ap_public_url` is what the task fetches, so it is set explicitly rather
    than left to the factory -- the task would otherwise request `None` and
    fail inside httpx rather than in the code under test.
    """
    instance = seed_community_owner(PEER)
    user = make_user(instance, name)
    user.ap_public_url = f'https://{PEER}/u/{name}'
    user.ap_profile_id = f'https://{PEER}/u/{name}'
    db.session.commit()
    return user


def _remote_community(name='memes'):
    """A remote community with nothing the task would fetch beyond the actor.

    `make_community` sets `ap_followers_url`; it is cleared so the followers
    collection is never fetched. `http_mock`'s `assert_all_called=True` would
    not catch that omission -- `block_outbound_http` would, by raising.
    """
    seed_community_owner(PEER)
    community = make_community(name, host=PEER)
    community.ap_public_url = f'https://{PEER}/c/{name}'
    community.ap_followers_url = None
    db.session.commit()
    return community


def _remote_feed(name='news'):
    """A REMOTE feed -- `make_local_feed` would give a local one, and
    `refresh_feed_profile_task` guards `not feed.is_local()`, so a local feed
    returns before fetching anything.
    """
    instance = seed_community_owner(PEER)
    feed = make_feed(instance, name)
    feed.ap_public_url = f'https://{PEER}/f/{name}'
    feed.ap_followers_url = None
    db.session.commit()
    return feed


def _person_document(name='wakko', fields=None):
    """The peer's Person document, holding only the keys the task reads
    unconditionally. Every other key is opted into via `fields`, so the absent
    side of each guard is what the baseline already gives.
    """
    document = {
        'type': 'Person',
        'id': f'https://{PEER}/u/{name}',
        'preferredUsername': name,
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


def _group_document(name='memes', fields=None):
    """The peer's Group document. `name` becomes the community title.

    `tag` is deliberately absent and must stay absent unless a test means to
    reach the legacy flair loop, which `tests/test_ap_refresh_community_profile.py`
    already covers and this file does not re-test.
    """
    document = {
        'type': 'Group',
        'id': f'https://{PEER}/c/{name}',
        'preferredUsername': name,
        'name': 'Memes, refreshed',
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


def _feed_document(name='news', fields=None):
    """The peer's Feed document."""
    document = {
        'type': 'Feed',
        'id': f'https://{PEER}/f/{name}',
        'preferredUsername': name,
        'name': 'News, refreshed',
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


def _serve(http_mock, url, document=None, status=200, text=None):
    """Register one respx route.

    `text` serves a raw body instead of JSON, which is how the malformed-JSON
    tests reach the `.json()` call with something that cannot be decoded.

    `http_mock` is created with `assert_all_called=True`, so every route
    registered here MUST be exercised by the test that registers it.
    """
    if text is not None:
        return http_mock.get(url).mock(
            return_value=httpx.Response(status, text=text))
    return http_mock.get(url).mock(
        return_value=httpx.Response(status, json=document))
```

**If `make_feed`'s signature differs** from `make_feed(instance, name)`, read `tests/factories.py` and follow it — sub-project 8 added `make_local_feed` precisely because `make_feed` sets `ap_id` unconditionally, so check which one gives a *remote* feed and use that.

- [ ] **Step 2: Write the user task's happy path**

```python
def test_refreshing_a_user_applies_the_peers_document(app, db_session, http_mock):
    """`refresh_user_profile_task`'s ordinary path: fetch the actor document
    and apply it.

    The assertion is on the User row's own columns, not on the absence of an
    exception. The task commits the refreshed profile before doing anything
    else, so "nothing raised" would not distinguish a working apply from one
    that abandoned the document.
    """
    user = _remote_user()
    _serve(http_mock, user.ap_public_url,
           _person_document(fields={'name': 'Wakko Warner'}))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.user_name == 'wakko'
    assert user.title == 'Wakko Warner'
```

**`db.session.refresh(user)` is load-bearing here** and unusually so: the task uses its **own** session from `get_task_session()`, not `db.session`, so the test's identity map holds a stale object no commit of the task's has expired. Confirm that by removing the refresh — if the test still passes, say so in your report, because it would mean the two sessions are sharing more than expected.

- [ ] **Step 3: Run the tests**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: scaffold the refresh-profile suite and cover the user task's happy path"
```

---

### Task 2: `refresh_user_profile_task`'s guards

**Files:**
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

The guard, located by content:

```python
if user and user.instance_id and user.instance.online():
```

**Three conjuncts.** Each needs its own test with the other two arranged true, or the mutations cannot be attributed.

- [ ] **Step 1: Write one test per conjunct**

```python
def test_refreshing_an_unknown_user_id_does_nothing(app, db_session, http_mock):
    """First conjunct: `user` is None when the id resolves to no row.

    No route is registered, so `block_outbound_http` would raise if the task
    fetched anything -- the absence of a request is asserted by the absence of
    a failure, which is why this test registers nothing.
    """
    _remote_user()

    refresh_user_profile_task(999999)


def test_refreshing_a_user_with_no_instance_does_nothing(app, db_session, http_mock):
    """Second conjunct: `user.instance_id`. NO FACTORY produces a NULL
    instance_id, so it is set explicitly here -- a test resting on the factory
    would never reach this branch.

    This conjunct is the one `refresh_community_profile_task` and
    `refresh_feed_profile_task` LACK, which is what makes them crash on the
    same row. Their pins are in Tasks 5 and 7.
    """
    user = _remote_user()
    user.instance_id = None
    db.session.commit()

    refresh_user_profile_task(user.id)


def test_refreshing_a_user_on_a_dormant_instance_does_nothing(app, db_session, http_mock):
    """Third conjunct: `user.instance.online()`, which is
    `not (dormant or gone_forever)` (app/models.py). `dormant` is set
    explicitly -- it defaults to False, so a test resting on the default
    would assert the wrong side of the branch.
    """
    user = _remote_user()
    user.instance.dormant = True
    db.session.commit()

    refresh_user_profile_task(user.id)
```

**These three tests register no routes deliberately.** `http_mock` is still requested so `assert_all_called=True` has nothing to complain about, and `block_outbound_http` raises if the task fetches. **If a test fails with an outbound-HTTP error, that is the finding** — it means the guard did not short-circuit — so report it rather than adding a route to make it pass.

- [ ] **Step 2: Mutation-test each conjunct separately**

| Mutation | Must fail | Must still pass |
|---|---|---|
| remove `user and` | `test_refreshing_an_unknown_user_id_does_nothing` | the other two |
| remove `user.instance_id and` | `test_refreshing_a_user_with_no_instance_does_nothing` | the other two |
| remove `and user.instance.online()` | `test_refreshing_a_user_on_a_dormant_instance_does_nothing` | the other two |

Each dropped conjunct **weakens** the guard, so the task proceeds where it should have returned and then fetches — which `block_outbound_http` turns into a raised error. **These are crash-kills, not assertion-kills**, and that is correct for this shape: the test asserts nothing after the call because the observable is that nothing happened. Say so in your report.

Restore after each and verify `git diff app/activitypub/util.py` is empty before committing.

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 4 passed.

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: isolate all three conjuncts of the user refresh guard"
```

---

### Task 3: `refresh_user_profile_task`'s error paths

**Files:**
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

- [ ] **Step 1: Write the retry test**

```python
def test_a_failed_fetch_is_retried_once(app, db_session, http_mock, no_real_sleeping):
    """`except httpx.HTTPError:` -> `time.sleep(randint(3, 10))` -> one retry.

    `no_real_sleeping` is REQUIRED: without it this test sleeps for up to ten
    real seconds. The task sleeps inline in the worker rather than deferring to
    the broker, which is registered as a finding rather than fixed here.

    respx serves a failure then a success from one route by giving `side_effect`
    a list, so the retry is the second call rather than a second route -- one
    route, two responses, which is also what `assert_all_called=True` expects.
    """
    user = _remote_user()
    http_mock.get(user.ap_public_url).mock(side_effect=[
        httpx.ConnectError('boom'),
        httpx.Response(200, json=_person_document(fields={'name': 'Retried'})),
    ])

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.title == 'Retried'
```

**If respx's `side_effect` list does not raise the first entry**, read respx's documentation for the version pinned in `requirements-test.txt` and follow what it actually supports — the shape matters less than that the first attempt fails and the second succeeds.

- [ ] **Step 2: Write the JSONDecodeError test — the finding's control**

```python
def test_a_malformed_actor_document_counts_an_instance_failure(
        app, db_session, http_mock):
    """PINS THE CORRECT BEHAVIOUR, which is the control for two defects.

    `refresh_user_profile_task` wraps `actor_data.json()` in
    `try/except JSONDecodeError`, increments `user.instance.failures` and
    returns. `refresh_community_profile_task` and `refresh_feed_profile_task`
    call `.json()` unguarded and raise instead -- pinned in Tasks 5 and 7 and
    fixed in Task 10.

    `failures` is seeded to 5 because it defaults to 0: an assertion of `1`
    against a default of `0` cannot tell an increment from an assignment, and
    an assertion of "not 0" cannot tell it from any other write.
    """
    user = _remote_user()
    user.instance.failures = 5
    db.session.commit()
    _serve(http_mock, user.ap_public_url, text='<html>not json</html>')

    refresh_user_profile_task(user.id)

    db.session.refresh(user.instance)
    assert user.instance.failures == 6
    assert user.title is None or user.title == ''
```

**Read what `user.title` actually is on a factory-built user** before asserting it — the point of the second assertion is that the document was NOT applied, so assert whatever "unapplied" really looks like rather than guessing.

- [ ] **Step 3: Write the non-200 test**

```python
def test_a_non_200_actor_response_applies_nothing(app, db_session, http_mock):
    """`if actor_data.status_code == 200:` -- a 404 from the peer leaves the
    row untouched. The title is asserted unchanged rather than the absence of
    an exception, because the task returns normally either way.
    """
    user = _remote_user()
    user.title = 'Before'
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document(), status=404)

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.title == 'Before'
```

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 7 passed.

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: cover the user refresh task's retry and error paths"
```

---

### Task 4: `refresh_user_profile_task`'s field application

**Files:**
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

**Read the task's body before writing these.** It applies a sequence of optional fields, each guarded by an `in activity_json` test. The three below are the ones with observable side effects beyond a column write; **cover every remaining optional field in the same shape**, one test per branch pair where the branch does something more than assign.

- [ ] **Step 1: Write the indexable test**

```python
def test_a_changed_indexable_flag_rewrites_the_users_posts(app, db_session, http_mock):
    """`new_indexable != user.indexable` runs raw SQL over every post the user
    has. The document omits `indexable`, which the task reads as True.

    A post is seeded with `indexable=False` so the UPDATE has something to
    change, and the assertion is on the POST row rather than the user -- the
    user's own column is not what this branch writes.
    """
    user = _remote_user()
    user.indexable = False
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.indexable is True
```

**Read whether the task also sets `user.indexable`** or only rewrites the posts. If it only rewrites posts, seed a `Post` via `make_post` and assert on `post.indexable` instead — and say in your report which it was, because the plan is asserting the user column on an assumption.

- [ ] **Step 2: Write the WordPress ap_id fix**

```python
def test_a_wordpress_style_ap_id_is_rewritten(app, db_session, http_mock):
    """`if user.ap_id.startswith('@'):` -- WordPress actors arrive with a
    leading '@', and the task rewrites the id from the profile URL.

    The '@' prefix is set explicitly; no factory produces one.
    """
    user = _remote_user()
    user.ap_id = f'@wakko@{PEER}'
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert not user.ap_id.startswith('@')
    assert user.ap_id == f'wakko@{PEER}'
```

- [ ] **Step 3: Write the summary/name pair**

```python
def test_optional_profile_fields_are_applied_when_present(app, db_session, http_mock):
    """The `if 'x' in activity_json` guards. Present here; their absent side is
    the baseline every other test in this file already exercises, since
    `_person_document` omits them.
    """
    user = _remote_user()
    _serve(http_mock, user.ap_public_url, _person_document(fields={
        'name': 'Wakko Warner',
        'summary': '<p>Faboo</p>',
    }))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.title == 'Wakko Warner'
    assert 'Faboo' in user.about_html


def test_an_absent_summary_clears_the_users_about_html(app, db_session, http_mock):
    """The `else` on `if 'summary' in activity_json:` -- an absent key does not
    leave `about_html` alone, it RESETS it to ''. The user is seeded with
    existing text so the reset is observable; without that seed this test
    could not tell a reset from a no-op.

    `_person_document` omits `summary`, so the baseline every other test in
    this file uses is already this branch -- but only this test asserts it.
    """
    user = _remote_user()
    user.about_html = '<p>Existing bio</p>'
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.about_html == ''
```

`summary` lands in `User.about_html` (`app/models.py:990`), via `allowlist_html`, and PeerTube's bare-text form is wrapped in `<p>` first — so assert on the sanitised result, not on the raw document value.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 11 passed, plus one per additional optional field you covered. (Seven extra fields were covered and a later fix round added a sibling, so the running total leaving this task is **19**, and every count below reflects that.)

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: cover the user refresh task's field application"
```

---

### Task 5: `refresh_community_profile_task` — happy path and both crash pins

**Files:**
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: `test_a_community_with_no_instance_crashes` and `test_a_malformed_community_document_crashes`, which **Task 10 inverts**. Task 10 needs those exact names.

**DO NOT FIX ANYTHING IN THIS TASK.** Both crashes are fixed in Task 10, in their own commits, so each fix has a witnessed pre-fix failure.

- [ ] **Step 1: Write the happy path, both ways in**

```python
def test_refreshing_a_community_applies_a_fetched_document(app, db_session, http_mock):
    """The fetch path: `activity_json` is falsy, so the task fetches."""
    community = _remote_community()
    _serve(http_mock, community.ap_public_url, _group_document())

    refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'


def test_refreshing_a_community_applies_a_supplied_document(app, db_session, http_mock):
    """The no-fetch path. `refresh_community_profile_task` is the ONLY one of
    the three that takes `activity_json`; the user and feed tasks take an id
    alone and always fetch. That asymmetry is registered, not fixed.

    No route is registered, and `block_outbound_http` would raise if the task
    fetched anyway -- so the absence of a request is what this test proves.
    """
    community = _remote_community()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'
```

- [ ] **Step 2: PIN the missing `instance_id` guard**

```python
def test_a_community_with_no_instance_crashes(app, db_session, http_mock):
    """PINS A CRASH. DO NOT FIX -- Task 10 does.

    `refresh_community_profile_task` opens
    `if community and community.instance.online():` with NO `instance_id`
    check, where `refresh_user_profile_task` guards
    `user and user.instance_id and user.instance.online()`. `instance_id` is a
    nullable FK (app/models.py), so a NULL one makes `community.instance` None
    and `.online()` raises.

    Two of the three tasks get this wrong and one gets it right, which is what
    makes it an oversight rather than a choice. The feed task's twin pin is in
    Task 7.
    """
    community = _remote_community()
    community.instance_id = None
    db.session.commit()

    with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'online'"):
        refresh_community_profile_task(community.id, _group_document())
```

**Run this one on its own and quote the exception text verbatim in your report.** Task 10 inverts it and needs to know exactly what it raises. If the message differs, use what it really says.

- [ ] **Step 3: PIN the unguarded `.json()`**

```python
def test_a_malformed_community_document_crashes(app, db_session, http_mock):
    """PINS A CRASH. DO NOT FIX -- Task 10 does.

    `refresh_community_profile_task` calls `actor_data.json()` unguarded,
    where `refresh_user_profile_task` wraps it in `try/except JSONDecodeError`,
    increments `instance.failures` and returns. So a peer that answers 200 with
    a non-JSON body raises out of this task and is merely counted for the user
    task -- and it is the peer that chooses the body.

    `test_a_malformed_actor_document_counts_an_instance_failure` is the
    control showing the handled shape.
    """
    community = _remote_community()
    _serve(http_mock, community.ap_public_url, text='<html>not json</html>')

    with pytest.raises(Exception):
        refresh_community_profile_task(community.id, None)
```

**`pytest.raises(Exception)` is too loose to ship.** Run it, see what is actually raised — a `json.JSONDecodeError`, an `httpx` error, or something else — and narrow the assertion to that class with a `match=`. Report what you found.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 23 passed.

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: cover the community refresh task and pin both its crashes"
```

---

### Task 6: `refresh_community_profile_task`'s remaining branches

**Files:**
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

- [ ] **Step 1: Write the moderators-source pair**

```python
def test_the_moderators_url_is_taken_from_attributed_to(app, db_session, http_mock):
    """`if 'attributedTo' in activity_json and isinstance(..., str):` -- the
    lemmy and mbin spelling. The url is served so the fetch it triggers has a
    route; `assert_all_called=True` means this test fails if the task does not
    actually fetch it.
    """
    community = _remote_community()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url, {'type': 'OrderedCollection', 'orderedItems': []})

    refresh_community_profile_task(
        community.id, _group_document(fields={'attributedTo': mods_url}))

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'


def test_the_moderators_url_falls_back_to_the_kbin_spelling(app, db_session, http_mock):
    """`elif 'moderators' in activity_json:` -- kbin's spelling. Reached only
    when `attributedTo` is absent or not a string, so the document carries
    `moderators` alone.
    """
    community = _remote_community()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url, {'type': 'OrderedCollection', 'orderedItems': []})

    refresh_community_profile_task(
        community.id, _group_document(fields={'moderators': mods_url}))

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'
```

**Read what the task does with `mods_url` before asserting only the title.** If it applies moderators to the community, assert that instead — the title assertion here is a placeholder proving the task completed, and a better assertion exists if the collection has an observable effect. Say which you chose.

- [ ] **Step 2: Write the nsfw/nsfl pair**

```python
def test_the_sensitive_flag_sets_nsfw(app, db_session, http_mock):
    """`community.nsfw = activity_json['sensitive'] if 'sensitive' in ... else False`
    -- note the else, which means an absent key RESETS nsfw rather than
    leaving it. The community is seeded nsfw=True so the reset is observable,
    and its twin below asserts the set.
    """
    community = _remote_community()
    community.nsfw = True
    db.session.commit()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.nsfw is False


def test_a_sensitive_document_sets_nsfw(app, db_session, http_mock):
    """The truthy side of the same expression."""
    community = _remote_community()
    community.nsfw = False
    db.session.commit()

    refresh_community_profile_task(
        community.id, _group_document(fields={'sensitive': True}))

    db.session.refresh(community)
    assert community.nsfw is True
```

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 27 passed.

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: cover the community refresh task's document branches"
```

---

### Task 7: `refresh_feed_profile_task` — happy path, `is_local`, and both crash pins

**Files:**
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: `test_a_feed_with_no_instance_crashes` and `test_a_malformed_feed_document_crashes`, which **Task 10 inverts**. Task 10 needs those exact names.

**DO NOT FIX ANYTHING IN THIS TASK.**

- [ ] **Step 1: Write the happy path**

```python
def test_refreshing_a_feed_applies_the_peers_document(app, db_session, http_mock):
    """`refresh_feed_profile_task` always fetches -- it takes a feed id alone,
    with no `activity_json` parameter, unlike the community task.
    """
    feed = _remote_feed()
    _serve(http_mock, feed.ap_public_url, _feed_document())

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
```

- [ ] **Step 2: Write the `is_local` guard — the conjunct only this task has**

```python
def test_refreshing_a_local_feed_does_nothing(app, db_session, http_mock):
    """`not feed.is_local()` -- the third conjunct, which NEITHER sibling has.
    A local feed returns before fetching, so no route is registered and
    `block_outbound_http` would raise if it fetched anyway.

    `make_local_feed` gives a local feed directly; using it rather than
    mutating a remote one keeps the fixture honest about what it represents.
    """
    from tests.factories import make_local_feed
    seed_community_owner(PEER)
    feed = make_local_feed('localnews')
    db.session.commit()

    refresh_feed_profile_task(feed.id)
```

**Move that import to the top of the file** with the others — it is inline here only to keep the snippet self-contained.

- [ ] **Step 3: PIN both crashes**

```python
def test_a_feed_with_no_instance_crashes(app, db_session, http_mock):
    """PINS A CRASH. DO NOT FIX -- Task 10 does.

    `refresh_feed_profile_task` opens
    `if feed and feed.instance.online() and not feed.is_local():` with no
    `instance_id` check. Same defect as the community task's, same cause, and
    the user task's guard is the shape both should have.
    """
    feed = _remote_feed()
    feed.instance_id = None
    db.session.commit()

    with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'online'"):
        refresh_feed_profile_task(feed.id)


def test_a_malformed_feed_document_crashes(app, db_session, http_mock):
    """PINS A CRASH. DO NOT FIX -- Task 10 does.

    `actor_data.json()` unguarded, exactly as in the community task. The user
    task's handler is the control.
    """
    feed = _remote_feed()
    _serve(http_mock, feed.ap_public_url, text='<html>not json</html>')

    with pytest.raises(Exception):
        refresh_feed_profile_task(feed.id)
```

**Narrow both `pytest.raises` to what is actually raised**, with a `match=`, and quote the real text in your report — Task 10 inverts both and needs it.

- [ ] **Step 4: Run and commit**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 31 passed.

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: cover the feed refresh task, its is_local guard and both crashes"
```

---

### Task 8: The retry path across all three, and the sleep

**Files:**
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

Task 3 covered the user task's retry. This task covers the other two and pins the difference in what they catch.

- [ ] **Step 1: Write the community and feed retry tests**

```python
def test_a_failed_community_fetch_is_retried_once(
        app, db_session, http_mock, no_real_sleeping):
    """The community task's retry, whose inner catch is `except Exception:`
    where the user task's is a bare `except:`. Both reach the retry; the
    difference is what happens when the RETRY fails, which the next test pins.
    """
    community = _remote_community()
    http_mock.get(community.ap_public_url).mock(side_effect=[
        httpx.ConnectError('boom'),
        httpx.Response(200, json=_group_document()),
    ])

    refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'


def test_a_community_whose_retry_also_fails_returns_quietly(
        app, db_session, http_mock, no_real_sleeping):
    """`except Exception: return` on the retry. The community keeps its
    pre-refresh title, which is the observable -- the task returns normally,
    so "nothing raised" would not distinguish this from a successful refresh.
    """
    community = _remote_community()
    community.title = 'Before'
    db.session.commit()
    http_mock.get(community.ap_public_url).mock(side_effect=[
        httpx.ConnectError('boom'),
        httpx.ConnectError('boom again'),
    ])

    refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    assert community.title == 'Before'


def test_a_failed_feed_fetch_is_retried_once(
        app, db_session, http_mock, no_real_sleeping):
    """The feed task's retry, identical in shape to the community task's."""
    feed = _remote_feed()
    http_mock.get(feed.ap_public_url).mock(side_effect=[
        httpx.ConnectError('boom'),
        httpx.Response(200, json=_feed_document()),
    ])

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
```

- [ ] **Step 2: Write the user task's signed-GET fallback — the path only it has**

```python
def test_a_user_fetch_failing_outside_httpx_falls_back_to_a_signed_get(
        app, db_session, http_mock, no_real_sleeping):
    """PINS AN ASYMMETRY. `refresh_user_profile_task` has a THIRD path its
    siblings lack: a bare `except:` that retries with `signed_get_request`
    against the site's private key. A peer requiring HTTP signatures to serve
    its actor document is therefore refreshable for users and not for
    communities or feeds.

    The bare `except:` is what makes this path reachable from a non-httpx
    error, and it is registered rather than fixed: a bare except also catches
    KeyboardInterrupt and SystemExit, so changing it changes which failures
    retry and which propagate.
    """
```

```python
    site = seed_signing_site()
    user = _remote_user()
    calls = []

    def exploding_get_request(uri, params=None, headers=None):
        raise RuntimeError('not an httpx error')

    def fake_signed_get(uri, private_key, key_id, **kwargs):
        calls.append((uri, private_key))
        return httpx.Response(200, json=_person_document(fields={'name': 'Signed'}))

    monkeypatch.setattr(ap_util, 'get_request', exploding_get_request)
    monkeypatch.setattr(ap_util, 'signed_get_request', fake_signed_get)

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert calls and calls[0][0] == user.ap_public_url
    assert calls[0][1] == site.private_key
    assert user.title == 'Signed'
```

Add `from app.activitypub import util as ap_util` to the imports, and request `monkeypatch` in the signature. Both names are bound into `app.activitypub.util` — `signed_get_request` at its line 25 import and `get_request` at its line 32 import — so patching them on that module is the campaign's binding-site convention, not a shortcut.

`RuntimeError` is the point: it is **not** an `httpx.HTTPError`, so `except httpx.HTTPError:` does not catch it and the bare `except:` does. That is the only way into this path, and it is why the bare `except:` is registered rather than fixed — narrowing it to `except Exception:` would keep this path, but narrowing it to the sibling's shape and no more would delete it.

**Do not use `http_mock` in this test.** `get_request` never runs, so no route is exercised, and `assert_all_called=True` would fail on any route you registered. **If patching `get_request` turns out not to reach the fallback** — say, because the task calls it through a different name — read the task and report what it actually calls, rather than forcing the fixture.

- [ ] **Step 3: Run and commit**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 35 passed.

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: cover all three retry paths and the user task's signed-GET fallback"
```

---

### Task 9: The collections, and what each task fetches beyond the actor

**Files:**
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

**Read all three tasks' tails before writing this.** Each fetches some subset of moderators, followers and featured, each gated on the corresponding URL being set. The tests below cover the community task; **write the equivalents for the feed task**, and if the user task fetches nothing beyond the actor, say so in your report — that is itself a difference worth recording.

- [ ] **Step 1: Write the followers-collection pair**

```python
def test_a_followers_url_is_fetched_and_counted(app, db_session, http_mock):
    """The followers collection is fetched only when `ap_followers_url` is set.
    `_remote_community` clears it, so this test sets it back -- which is the
    contrast that makes the guard killable.
    """
    community = _remote_community()
    followers_url = f'https://{PEER}/c/memes/followers'
    community.ap_followers_url = followers_url
    db.session.commit()
    _serve(http_mock, followers_url,
           {'type': 'Collection', 'totalItems': 42})

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.subscriptions_count == 42


def test_no_followers_url_means_no_followers_fetch(app, db_session, http_mock):
    """The absent side. No route is registered for the followers collection,
    and `block_outbound_http` raises if the task fetches one -- so this test
    proves the guard short-circuits rather than merely that a count stayed put.
    """
    community = _remote_community()
    community.subscriptions_count = 7
    db.session.commit()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.subscriptions_count == 7
```

`Community.subscriptions_count` (`app/models.py:568`) is the column; confirm the task writes the follower count there and not somewhere else before relying on it.

- [ ] **Step 2: Run and commit**

Run: `./run_tests.sh tests/test_ap_refresh_profiles.py -q`
Expected: 37 passed, plus one per feed-side equivalent you added.

```bash
git add tests/test_ap_refresh_profiles.py
git commit -m "test: cover the opt-in collection fetches"
```

---

### Task 10: Fix both crash paths

**Files:**
- Modify: `app/activitypub/util.py`
- Modify: `tests/test_ap_refresh_profiles.py`

**Interfaces:**
- Consumes: `test_a_community_with_no_instance_crashes`, `test_a_malformed_community_document_crashes`, `test_a_feed_with_no_instance_crashes`, `test_a_malformed_feed_document_crashes` from Tasks 5 and 7.
- Produces: nothing later tasks need.

**This is the only task in the sub-project that changes production code. TWO FIXES, TWO SEPARATE COMMITS.**

Both fixes take the shape `refresh_user_profile_task` already uses. **Match it rather than inventing a third spelling** — it is the one of the three that gets both right, in the same file.

**Fix A — the missing `instance_id` guard.** Add it to `refresh_community_profile_task` and `refresh_feed_profile_task`, in the position the user task has it. Inverts `test_a_community_with_no_instance_crashes` and `test_a_feed_with_no_instance_crashes`.

**Fix B — the unguarded `.json()`.** Wrap it in `try/except JSONDecodeError`, increment the instance's `failures` and return, exactly as the user task does. Inverts `test_a_malformed_community_document_crashes` and `test_a_malformed_feed_document_crashes`.

**Note Fix B gains behaviour, not just safety:** the two tasks will start counting instance failures they previously crashed on. That is intended and is what the user task already does.

For each fix, four steps:

- [ ] **Fix A — Step 1:** Rename both `..._crashes` pins to `..._is_skipped`, replace the `pytest.raises` blocks with plain calls, and assert the row was **not** modified — a bare "nothing raised" would pass against a fix that returned early for the wrong reason. Rewrite both docstrings so they describe the fix in the past tense.
- [ ] **Fix A — Step 2:** Run both and watch them FAIL. **Quote both failures verbatim in your report.**
- [ ] **Fix A — Step 3:** Apply the guard to both tasks; run; expect green.
- [ ] **Fix A — Step 4:** Mutation-test: remove the guard from the community task alone, confirm only its test fails; restore; remove it from the feed task alone, confirm only its test fails; restore. **Two call sites, two separate kills** — this campaign has hit a one-clause-two-sites gap three times. Then commit:

```bash
git add app/activitypub/util.py tests/test_ap_refresh_profiles.py
git commit -m "fix: skip refreshing an actor whose instance is unknown"
```

- [ ] **Fix B — Steps 5-8:** the same four steps for the `.json()` guard. The inverted tests assert `instance.failures` incremented from a seeded non-zero baseline, matching `test_a_malformed_actor_document_counts_an_instance_failure`. Commit:

```bash
git commit -m "fix: count a malformed actor document as an instance failure"
```

- [ ] **Step 9: Check for a vacated branch.** Each inversion may have removed the only test reaching some branch. For each inverted test, name the branch it used to cover and the test that covers it now. If none does, add one and mutation-prove it.

- [ ] **Step 10: Audit docstrings falsified by the fixes.** Every claim in the file that the community or feed task crashes is now false — including `test_a_malformed_actor_document_counts_an_instance_failure`'s docstring, which says the siblings "raise instead", and the user-guard tests in Task 2 which say the siblings lack the `instance_id` check. Grep for `crash`, `raise`, `unguarded`, `LACK`, `wrong`, `oversight`, and check every hit. **Sub-projects 10, 11 and 12 each lost review rounds to exactly this.**

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

Read the SDD ledger at `.superpowers/sdd/2026-09-03-coverage-refresh-profiles-13/progress.md` in full, then the per-task reports in the same directory, then the spec. **Register what was found, not what was predicted.**

- [ ] **Step 2: Write the register entries, numbered from D213**

Match the surrounding entries' house style — read several existing D-entries first. Each states the defect, where it lives (by content, with a line citation correct **at your commit**), how it was found, whether it was fixed or registered, and if registered, **why not fixed**.

At minimum, and the ledger will have more:

1. **The community and feed tasks crashed on an actor with a NULL `instance_id`** — FIXED, with its commit. Record that the user task guarded it and the other two did not: two of three wrong, one right.
2. **The community and feed tasks crashed on malformed JSON from a peer** — FIXED, with its commit. Record that this was remotely triggerable by any peer being refreshed from, and that the fix also gains the failure-counting the user task already did.
3. **`refresh_user_profile_task` uses a bare `except:`** where its siblings use `except Exception:` — it catches `KeyboardInterrupt` and `SystemExit`, so a worker shutdown mid-refresh is swallowed into the signed-GET fallback.
4. **Only the user task has a `signed_get_request` fallback**, so a signature-requiring peer is refreshable for users and not for communities or feeds.
5. **Only the feed task checks `is_local()`.**
6. **Only the community task takes `activity_json`**, so only it can be driven from a document a caller already holds.
7. **All three sleep `randint(3, 10)` seconds inline** in a Celery worker rather than deferring the retry to the broker.

- [ ] **Step 3: Update BOTH live "Next free number" notes.** Find every occurrence; the two carrying the campaign's current value must both change. The historical ones are frozen — leave them alone.

- [ ] **Step 4: Add the harness facts to `tests/README.md`.** Read the file first and match its style; the numbered facts currently run to 62. Add only what is not already there. Candidates:

- **`get_request()` rejects domains ending in `.local`** via `is_invalid_get_request_uri()`, before respx sees the request — a `.local` fixture fails for a reason unrelated to the code under test.
- **`http_mock` uses `assert_all_called=True`**, so a route registered and never exercised fails the test.
- **A Celery task in this codebase is tested by calling it directly.** Callers invoke inline under `current_app.debug` and the `app` fixture puts celery in eager mode.
- **`get_task_session()` leaves autoflush at `True`** where `db.session` is configured `autoflush=False`, so a task's session and the test's session behave differently.
- **A task using `get_task_session()` commits on its own session**, so a test holding the row from `db.session` needs `db.session.refresh()` to see the change.
- Anything else in the ledger's rulings that generalises.

- [ ] **Step 5: Raise the floor.** `coverage_floors.ini`, `app/activitypub/util.py`: raise from 55 to the measured blended figure rounded down. **The controller supplies that figure — do not run a coverage run.**

- [ ] **Step 6: Verify every citation.** Task 10's fixes shifted lines in `app/activitypub/util.py`. Verify every `app/*.py` line citation you wrote against current source. Stale citations are this campaign's most frequent defect.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 13's findings and raise the util.py floor"
```

---

## Self-review

**Spec coverage.** Each of the spec's three functions has tasks: user Tasks 1-4, community Tasks 5-6, feed Task 7, all three Tasks 8-9. Every row of the spec's asymmetry table is reached: the guard's three conjuncts (Task 2), `instance_id` (Tasks 5, 7, 10), `is_local` (Task 7), `activity_json` (Task 5), always-fetches (Tasks 5, 7), retry catch (Task 8), signed-GET fallback (Task 8), `.json()` decode (Tasks 3, 5, 7, 10). The spec's eight success criteria map to Tasks 1-9 (1-3), Task 10 (4), Task 11 (5-7) and the controller's final run (8).

**Placeholder scan.** No "TBD", no "add appropriate error handling". Every code step carries real code. **Eight steps deliberately say "read X and follow what it actually does"** — those are falsifiable checks against a stated expectation, each naming what to read and what to report. Three of them (`user.title`'s unapplied value, the `summary` column, `subscriptions_count`) mark places where the plan is guessing a column name and says so; the implementer must replace the guess, not ship it.

**Type consistency.** `_remote_user`/`_remote_community`/`_remote_feed` return a single row; `_person_document`/`_group_document`/`_feed_document` take `(name, fields)`; `_serve(http_mock, url, document=None, status=200, text=None)` is called with that signature in every task. The four `..._crashes` names Tasks 5 and 7 produce are the four Task 10 consumes, and Task 10 states the new names.

**One gap found and fixed inline:** Task 10 originally had one commit for both fixes; the plan's own Global Constraints require one commit per defect, so it is now two, with the mutation step naming both call sites per fix — the one-clause-two-sites gap this campaign has hit three times.

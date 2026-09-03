# Sub-project 11: content-object endpoint coverage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `comment_ap`, `post_ap`, `post_replies_ap`, `post_ap_context`, `activities_json` and `activity_result` to full statement coverage, fix `post_replies_ap`'s 500, and register the rest.

**Architecture:** One new test file driving all six endpoints through `app.test_client()` with a real `Accept` header. The four content-object endpoints are compared against each other by design — the asymmetries are the findings. Delegates are patched at the `app.activitypub.routes` binding site.

**Tech Stack:** pytest, Flask test client, SQLAlchemy, `tests/factories.py`.

**Spec:** `docs/superpowers/specs/2026-09-03-coverage-content-objects-11-design.md`

## Global Constraints

- Defects found **inside these six functions** are fixed test-first, each in its own commit, each proved by a mutation that fails a named test. Anything outside them is **registered, not fixed**.
- Findings are numbered from **D187**, and **both** of the register's "Next free number" notes are updated in the same change.
- The coverage floor rises to the measured blended figure rounded down. Current floor 86, measured 86.2221%.
- **Locate every code target by content, not by the line numbers in this plan.** Task 10's fix will shift lines.
- **Every test asserts `response.status_code`.** Success paths also assert `content_type` and `Cache-Control`, plus `Vary` and `Link` where the endpoint sets them.
- **No vacuous assertions.** Never assert a value equal to a column's declared default without seeding a contrary baseline, and never compare a response value to `obj.<column>` when the factory may leave that column `None` (harness fact 50).
- **A crash is a raised exception, not a 500 response** (harness fact 43). Pin with `pytest.raises`, never `assert response.status_code == 500`.
- The full suite must pass. **Only the controller runs it, one session at a time**, and the controller supplies every coverage figure. Do not run a coverage run.
- **Delete nothing** the task did not create. `claude_test` in the repository root is not the campaign's.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/test_ap_content_objects.py` | **Create.** All tests for the six endpoints. |
| `tests/factories.py` | **Modify.** Add `make_activitypub_log`. |
| `app/activitypub/routes.py` | **Modify, Task 10 only.** `post_replies_ap`'s missing `else`. |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | **Modify, Task 11 only.** |
| `tests/README.md` | **Modify, Task 11 only.** |
| `coverage_floors.ini` | **Modify, Task 11 only.** |

## Helpers each task inherits

SDD implementers see only their own task brief, so this table is repeated into every brief. Task 1 creates all of these; Tasks 2-9 import and use them, and **must not reimplement them**.

| Name | Where | Signature / behaviour |
|---|---|---|
| `AP_ACCEPT` | this file | `'application/activity+json'` |
| `ap_get(app, path, user_agent=None)` | this file | GET with the AP `Accept` header; sets `User-Agent` when given |
| `browser_get(app, path)` | this file | GET with **no** `Accept` header — the non-AP branch |
| `seed_actors(host='peer.example')` | `tests/test_actor_profiles.py` | returns `(site, instance)`; creates Site, Instance id 1, User id 1 |
| `seed_local_post(community=None, user=None, **kw)` | this file | returns `(community, author, post)` for a **local** post (`ap_id=None`) |
| `_double_the_delegates(monkeypatch)` | this file | patches the five delegates; returns a `calls` dict |
| `make_activitypub_log(...)` | `tests/factories.py` | Task 1 adds it; see Task 1 Step 3 for the exact signature |
| `make_post(community, user, ap_id, title='a post', private=False, microblog=False)` | `tests/factories.py` | **`ap_id` is positional and required.** Pass `None` for a local post. Sets `deleted=False`, leaves `status` at `POST_STATUS_PUBLISHED`. |
| `make_post_reply(post, user, body='a reply')` | `tests/factories.py` | sets `deleted=False` explicitly |
| `make_community(name='microblogs', host='test.piefed.local')` | `tests/factories.py` | local community |
| `make_user(instance, name, local=False, with_keys=False)` | `tests/factories.py` | **leaves `ap_profile_id`, `ap_public_url`, `ap_id` all `None` when `local=True`** |
| `make_instance(domain, software='mastodon')` | `tests/factories.py` | |
| `make_instance_block(user, instance)` | `tests/factories.py` | the row `has_blocked_instance` reads |

## Facts every task needs

1. **`is_activitypub_request()`** is `'application/ld+json' in Accept or 'application/activity+json' in Accept` (`app/activitypub/util.py`). There is a second, byte-identical definition in `app/utils.py` with **no importers** — cite the `activitypub/util.py` one.
2. **`requestor_domain()`** (`app/utils.py`) returns `''` unless the `User-Agent` contains a `+`; it takes the text after the last `+`, strips a trailing `)`, and runs it through `furl(...).host`. So `'Test (+https://peer.example)'` yields `'peer.example'`.
3. **`find_instance_id(server)`** returns `None` for a falsy server, otherwise looks up `Instance.domain` and **creates and commits a new sparse `Instance` row if not found**.
4. **`has_blocked_instance(None)` returns `False`**, so the 401 branch is unreachable without a `+`-style User-Agent.
5. **`Post.is_local()`** is `ap_id is None or ap_id.startswith(SERVER_URL)`.
6. **`POST_STATUS_PUBLISHED = 1`, `POST_STATUS_REVIEWING = 0`, `POST_STATUS_DRAFT = -1`, `POST_STATUS_SCHEDULED = -2`** (`app/constants.py`).
7. **Flask-Compress appends `Accept-Encoding` to every response's `Vary`.** `Vary` is never absent and never bare. The `Accept, User-Agent` branch is observed as `'Accept, User-Agent, Accept-Encoding'`.
8. **`CACHE_TYPE='NullCache'`** in `tests/conftest.py`, so `@cache.cached` is inert.
9. **Flask-Login caches on `g`**, and the `app` fixture pushes one application context per test, so a test cannot log in after making an earlier request in the same test. None of these six reads `current_user` except `post_ap`'s non-AP `sort` expression, which handles anonymous.

---

### Task 1: Scaffold, helpers, the `ActivityPubLog` factory, and `comment_ap`'s happy path

**Files:**
- Create: `tests/test_ap_content_objects.py`
- Modify: `tests/factories.py`
- Test: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AP_ACCEPT`, `ap_get`, `browser_get`, `seed_local_post`, `_double_the_delegates`, and `make_activitypub_log` in `tests/factories.py`. Every later task uses these.

- [ ] **Step 1: Write the file header and helpers**

```python
"""tests/test_ap_content_objects.py"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.constants import POST_STATUS_PUBLISHED, POST_STATUS_REVIEWING
from tests.factories import (make_activitypub_log, make_community, make_instance,
                             make_instance_block, make_post, make_post_reply, make_user)
from tests.test_actor_profiles import seed_actors

AP_ACCEPT = 'application/activity+json'


def ap_get(app, path, user_agent=None):
    """GET an endpoint as a remote ActivityPub client.

    `is_activitypub_request()` (app/activitypub/util.py) is a substring test on
    the Accept header, so a real header is sent rather than the function being
    doubled -- the parse is part of what is under test.

    `user_agent` is separate because `requestor_domain()` (app/utils.py) reads
    it and returns '' unless it contains a '+'. Since `find_instance_id('')`
    returns None and `has_blocked_instance(None)` returns False, the 401
    instance-block branch in `comment_ap` and `post_ap` is UNREACHABLE without
    a '+'-style agent string. A test that omits it measures the wrong branch.
    """
    headers = {'Accept': AP_ACCEPT}
    if user_agent is not None:
        headers['User-Agent'] = user_agent
    with app.test_client() as client:
        return client.get(path, headers=headers)


def browser_get(app, path):
    """GET with no Accept header at all -- the non-ActivityPub branch.

    Four of these endpoints answer this differently: `comment_ap` delegates to
    `continue_discussion`, `post_ap` to `show_post`, `post_ap_context` aborts
    400, and `post_replies_ap` falls off the end of the function entirely.
    """
    with app.test_client() as client:
        return client.get(path)


def seed_local_post(community=None, user=None, title='a post'):
    """A LOCAL post (`ap_id=None`) in a local community, with a local author.

    `ap_id=None` is what makes `Post.is_local()` true (app/models.py), which is
    the branch `post_ap` serves rather than 301-redirecting. `make_post` takes
    `ap_id` as a REQUIRED POSITIONAL, so it cannot be omitted.
    """
    site, instance = seed_actors()
    community = community or make_community(name='books', host='test.piefed.local')
    user = user or make_user(instance, 'poster', local=True)
    post = make_post(community, user, None, title=title)
    db.session.commit()
    return community, user, post


def _double_the_delegates(monkeypatch):
    """Stop the five delegates from running, and record what they were passed.

    All five are imported INTO `app.activitypub.routes` -- `post_to_page`,
    `comment_model_to_json` and `post_replies_for_ap` from
    `app.activitypub.util`, `continue_discussion` and `show_post` from
    `app.post.routes`, and `block_honey_pot` from `app.utils` -- so they are
    patched on that module, following this campaign's binding-site convention.
    `show_post` and `continue_discussion` render templates and must not run.
    """
    calls = {}
    for name, result in (('post_to_page', {'type': 'Page', 'id': 'https://test.piefed.local/post/1'}),
                         ('comment_model_to_json', {'type': 'Note', 'id': 'https://test.piefed.local/comment/1'}),
                         ('post_replies_for_ap', [{'type': 'Note', 'id': 'https://test.piefed.local/comment/1'}])):
        calls[name] = []
        monkeypatch.setattr(activitypub_routes, name,
                            lambda obj, _n=name, _r=result: calls[_n].append(obj) or dict(_r) if isinstance(_r, dict) else calls[_n].append(obj) or list(_r))
    for name in ('continue_discussion', 'show_post'):
        calls[name] = []
        monkeypatch.setattr(activitypub_routes, name,
                            lambda *a, _n=name, **kw: calls[_n].append((a, kw)) or f'HTML:{_n}')
    calls['block_honey_pot'] = []
    monkeypatch.setattr(activitypub_routes, 'block_honey_pot',
                        lambda: calls['block_honey_pot'].append(True))
    return calls
```

**A note on the `_double_the_delegates` lambda:** the conditional expression in
the first loop is deliberately ugly because `post_replies_for_ap` returns a
**list** and the other two return **dicts**, and each call must return a fresh
object so one test cannot mutate another's. If you find a cleaner spelling that
preserves both properties, use it — but verify both properties hold.

- [ ] **Step 2: Run the file to confirm it imports**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: FAIL — `ImportError: cannot import name 'make_activitypub_log'`. That is the next step.

- [ ] **Step 3: Add `make_activitypub_log` to `tests/factories.py`**

Place it beside the other factories, in the same style. The model is
`ActivityPubLog` in `app/models.py` with columns `direction`, `activity_id`,
`activity_type`, `activity_json`, `result`, `exception_message`, `created_at`.

```python
def make_activitypub_log(activity_id: str, *, direction: str = 'in',
                         activity_type: str = 'Create', activity_json: str = None,
                         result: str = 'success', exception_message: str = None) -> ActivityPubLog:
    """The row `activities_json` and `activity_result` both read.

    `activity_id` is the FULL URI both endpoints match on, not a bare id:
    `activities_json` builds `f"{SERVER_URL}/activities/{type}/{id}"` and
    `activity_result` builds `f'https://{id}'` from a path parameter, so a
    caller must pass whichever form the endpoint under test will construct.

    `activity_json` is a JSON **string**, not a dict -- `activities_json` calls
    `json.loads` on it -- and it is nullable, which is a branch that endpoint
    takes (`activity_json = {}`). `result` and `exception_message` are what
    `activity_result` branches on; `exception_message` defaults to None so a
    test asserting on the disclosure must set it explicitly rather than rest
    on a default.
    """
    log = ActivityPubLog(
        direction=direction,
        activity_id=activity_id,
        activity_type=activity_type,
        activity_json=activity_json,
        result=result,
        exception_message=exception_message,
        created_at=utcnow(),
    )
    db.session.add(log)
    db.session.commit()
    return log
```

Add `ActivityPubLog` to the `from app.models import ...` block at the top of
`tests/factories.py` if it is not already there.

- [ ] **Step 4: Write `comment_ap`'s happy path**

```python
def test_a_comment_is_served_as_activitypub_json(app, db_session, monkeypatch):
    """`comment_ap`'s ordinary path. Unlike `post_ap` it has NO `is_local()`
    check, so it serves ANY reply it can resolve -- including a remote one,
    which is registered rather than pinned here.

    `Cache-Control` is 120, matching `post_ap` and differing from
    `post_replies_ap` and `post_ap_context`, which both use 15.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=120'
    assert response.json['type'] == 'Note'
    assert calls['comment_model_to_json'] == [reply]
```

- [ ] **Step 5: Write the `Link` and `Vary` assertions**

```python
def test_a_comment_response_sets_its_link_and_vary_headers(app, db_session, monkeypatch):
    """`Link` points at the HTML alternate; `Vary` is `Accept` because this
    author has blocked no instances.

    `Vary` is asserted as `'Accept, Accept-Encoding'`, NOT `'Accept'`:
    Flask-Compress appends `Accept-Encoding` to every response, so the header
    is never bare (harness fact 38). The two-instance-block branch is
    `test_a_comment_from_a_blocking_author_varies_on_user_agent`.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 200
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'
    assert response.headers['Link'] == \
        f'<https://test.piefed.local/comment/{reply.id}>; rel="alternate"; type="text/html"'
```

**If the `Link` host is not `test.piefed.local`**, read `SERVER_NAME` out of
`tests/conftest.py` and use the real value — do not weaken the assertion to a
substring match.

- [ ] **Step 6: Write the unknown-comment 404**

```python
def test_an_unknown_comment_is_404(app, db_session, monkeypatch):
    """`PostReply.query.get_or_404` fires before any branch, so this is a 404
    for an ActivityPub request and a browser request alike -- unlike
    `post_replies_ap`, whose lookup sits INSIDE its `is_activitypub_request()`
    branch and is therefore never reached by a browser.
    """
    _double_the_delegates(monkeypatch)
    seed_actors()

    response = ap_get(app, '/comment/999999')

    assert response.status_code == 404
```

- [ ] **Step 7: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
git add tests/test_ap_content_objects.py tests/factories.py
git commit -m "test: scaffold the content-object suite and cover comment_ap's happy path"
```

---

### Task 2: `comment_ap`'s guards

**Files:**
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: `ap_get`, `browser_get`, `seed_local_post`, `_double_the_delegates` from Task 1.
- Produces: nothing later tasks need.

`comment_ap`'s guards, located by content in `app/activitypub/routes.py`:

```python
if reply.community.local_only or reply.community.private:
    abort(403)
if reply.author.has_blocked_instance(find_instance_id(requestor_domain())):
    return make_response(f'Author has blocked {requestor_domain()}'), 401
```

- [ ] **Step 1: Write the two 403 tests, one per disjunct**

Both flags must be set **explicitly on both sides**. `Community.local_only` and
`Community.private` both default to `False`, so a test that sets only one and
leaves the other implicit cannot prove which disjunct it killed.

```python
def test_a_comment_in_a_local_only_community_is_403(app, db_session, monkeypatch):
    """First disjunct of `if reply.community.local_only or reply.community.private`.

    `private` is set to False explicitly, not left at its column default, so
    dropping the `local_only` disjunct fails THIS test and not its twin.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = True
    community.private = False
    db.session.commit()
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 403


def test_a_comment_in_a_private_community_is_403(app, db_session, monkeypatch):
    """Second disjunct. `local_only` is set to False explicitly for the same
    reason its twin sets `private` explicitly.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = False
    community.private = True
    db.session.commit()
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 403
```

- [ ] **Step 2: Mutation-test both disjuncts**

Change the guard to `if reply.community.private:` — confirm
`test_a_comment_in_a_local_only_community_is_403` fails and its twin passes.
Then `if reply.community.local_only:` — confirm the reverse. Restore, and
**verify `git diff app/activitypub/routes.py` is empty** before committing.
Report both kills, and whether each was an assertion failure or a crash.

- [ ] **Step 3: Write the instance-block 401**

```python
def test_a_comment_is_401_when_the_author_has_blocked_the_requesting_instance(app, db_session, monkeypatch):
    """The 401 branch, and it is UNREACHABLE without a '+'-style User-Agent.

    `requestor_domain()` (app/utils.py) returns '' unless the agent string
    contains a '+', `find_instance_id('')` returns None, and
    `has_blocked_instance(None)` returns False. So this test sends
    'Test (+https://blocked.example)', from which `requestor_domain()` extracts
    'blocked.example' -- and the Instance row must already exist with that
    domain, or `find_instance_id` would CREATE one (and return an id the
    author has not blocked).
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    blocked = make_instance('blocked.example')
    make_instance_block(author, blocked)
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}',
                      user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401
    assert b'blocked.example' in response.data
```

- [ ] **Step 4: Write the negative half of the same guard**

```python
def test_a_comment_is_served_when_the_author_blocked_a_different_instance(app, db_session, monkeypatch):
    """The block is per-instance, not a global flag. The author blocks
    'other.example' and the request arrives from 'peer.example', so the 401
    must NOT fire -- this is what stops the guard being satisfied by any block
    at all, and it is a different assertion from the `has_blocked_instances()`
    Vary branch below, which IS a global flag.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    other = make_instance('other.example')
    make_instance_block(author, other)
    make_instance('peer2.example')
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}',
                      user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200
```

- [ ] **Step 5: Write the `Vary: Accept, User-Agent` branch**

```python
def test_a_comment_from_a_blocking_author_varies_on_user_agent(app, db_session, monkeypatch):
    """`if reply.author.has_blocked_instances():` -- a GLOBAL "does this author
    block anyone at all" flag, distinct from the per-instance
    `has_blocked_instance(id)` the 401 uses. The response varies on User-Agent
    because the body now depends on who is asking.

    The author blocks 'other.example' while the request comes from
    'peer2.example', so the 401 does NOT fire and this test reaches the header
    -- which is exactly what makes it discriminate the two different methods.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    other = make_instance('other.example')
    make_instance_block(author, other)
    make_instance('peer2.example')
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}',
                      user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200
    assert response.headers['Vary'] == 'Accept, User-Agent, Accept-Encoding'
```

- [ ] **Step 6: Write the non-ActivityPub delegation**

```python
def test_a_browser_request_for_a_comment_delegates_to_the_discussion_view(app, db_session, monkeypatch):
    """`comment_ap`'s else branch calls `continue_discussion(reply.post.id,
    comment_id)`. It is asserted to receive the POST's id and the COMMENT's id,
    in that order -- the two are different rows and swapping them is a real
    regression this assertion catches.

    `post_replies_ap` has NO else branch at all and crashes here; that is
    pinned by `test_a_browser_request_for_post_replies_crashes` and fixed in a
    later task.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    response = browser_get(app, f'/comment/{reply.id}')

    assert response.status_code == 200
    assert calls['continue_discussion'] == [((post.id, reply.id), {})]
```

- [ ] **Step 7: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 10 passed.

- [ ] **Step 8: Commit**

```bash
git add tests/test_ap_content_objects.py
git commit -m "test: cover comment_ap's visibility, instance-block and delegation guards"
```

---

### Task 3: `post_ap`'s ActivityPub happy path and headers

**Files:**
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

- [ ] **Step 1: Write the happy path**

```python
def test_a_local_post_is_served_as_activitypub_json(app, db_session, monkeypatch):
    """`post_ap`'s ordinary path: a GET with an ActivityPub Accept header for a
    LOCAL post. `post_to_page` is doubled and the route adds `@context` to what
    it returns, so the assertion covers both the delegation and the wrapping.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=120'
    assert response.json['type'] == 'Page'
    assert '@context' in response.json
    assert calls['post_to_page'] == [post]
```

- [ ] **Step 2: Write the two `Link` branches, one per side**

`post_ap` branches on `if post.slug:`. `Post.slug` is not set by `make_post`,
so the falsy side is the factory default and the truthy side must be set
explicitly. **Both are asserted**, because the two produce different URLs.

```python
def test_a_post_without_a_slug_links_to_its_numeric_url(app, db_session, monkeypatch):
    """The `else` half of `if post.slug:`. `make_post` never sets `slug`, so
    this is the factory's state -- and it is asserted rather than assumed
    because its twin below sets one explicitly and gets a different URL.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert response.headers['Link'] == \
        f'<https://test.piefed.local/post/{post.id}>; rel="alternate"; type="text/html"'


def test_a_post_with_a_slug_links_to_its_slug_url(app, db_session, monkeypatch):
    """The truthy half. The route interpolates the slug DIRECTLY after the host
    with no separator, so a slug must begin with '/' to produce a valid URL --
    which is itself worth knowing and is asserted here rather than papered over.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.slug = '/c/books/p/1/a-post'
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert response.headers['Link'] == \
        '<https://test.piefed.local/c/books/p/1/a-post>; rel="alternate"; type="text/html"'
```

`Post.slug` is `db.Column(db.String(255))` (`app/models.py:1710`), unset by
`make_post`, so the falsy side is genuinely the factory's state. The route
interpolates it directly after the host with no separator
(`f'<https://{SERVER_NAME}{post.slug}>'`), which is why the truthy fixture's
slug starts with `/`.

- [ ] **Step 3: Write the `Vary` two-branch pair**

```python
def test_a_post_from_a_non_blocking_author_varies_on_accept_only(app, db_session, monkeypatch):
    """`Vary` is `Accept` plus Flask-Compress's `Accept-Encoding`. The author
    blocks nobody, which is `make_user`'s state and is made contrary by the
    twin below rather than being asserted bare.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'


def test_a_post_from_a_blocking_author_varies_on_user_agent(app, db_session, monkeypatch):
    """`if post.author.has_blocked_instances():` -- the global flag. The author
    blocks 'other.example' and the request arrives from 'peer2.example', so the
    401 does not fire and the header branch is reached.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    other = make_instance('other.example')
    make_instance_block(author, other)
    make_instance('peer2.example')

    response = ap_get(app, f'/post/{post.id}',
                      user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200
    assert response.headers['Vary'] == 'Accept, User-Agent, Accept-Encoding'
```

- [ ] **Step 4: Write the unknown-post 404**

```python
def test_an_unknown_post_is_404_for_an_activitypub_request(app, db_session, monkeypatch):
    """`Post.query.get_or_404` sits INSIDE the `is_activitypub_request()`
    branch, so this 404 is reached only for an ActivityPub request. A browser
    request for the same id goes to `show_post` instead, which is doubled.
    """
    _double_the_delegates(monkeypatch)
    seed_actors()

    response = ap_get(app, '/post/999999')

    assert response.status_code == 404
```

- [ ] **Step 5: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 16 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_ap_content_objects.py
git commit -m "test: cover post_ap's activitypub path, Link and Vary branches"
```

---

### Task 4: `post_ap`'s visibility guards

**Files:**
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

The guard, located by content:

```python
if post.community.local_only or post.community.private or post.status < POST_STATUS_PUBLISHED:
    abort(403)
```

**Three disjuncts. Each needs its own test, with the other two set to their
non-triggering values explicitly.** This is the guard the whole task exists for.

- [ ] **Step 1: Write the three 403 tests**

```python
def test_a_post_in_a_local_only_community_is_403(app, db_session, monkeypatch):
    """First disjunct of three. `private` and `status` are both set explicitly
    to their non-triggering values, so dropping `local_only` fails THIS test
    alone.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = True
    community.private = False
    post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 403


def test_a_post_in_a_private_community_is_403(app, db_session, monkeypatch):
    """Second disjunct."""
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = False
    community.private = True
    post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 403


def test_an_unpublished_post_is_403(app, db_session, monkeypatch):
    """Third disjunct: `post.status < POST_STATUS_PUBLISHED`.

    `POST_STATUS_REVIEWING` is 0 and `POST_STATUS_PUBLISHED` is 1
    (app/constants.py), and `make_post` leaves `status` at the column default,
    which IS `POST_STATUS_PUBLISHED` -- so this test must set it, and both
    community flags are set to False so the other two disjuncts cannot be what
    produced the 403.

    Note `post_replies_ap` and `post_ap_context` apply NO status guard, so the
    same under-review post's replies remain enumerable. Registered, not fixed.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = False
    community.private = False
    post.status = POST_STATUS_REVIEWING
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 403
```

- [ ] **Step 2: Mutation-test all three disjuncts, one at a time**

Drop each disjunct from the guard separately — leaving the other two — and
confirm exactly one named test fails each time:

| Mutation | Must fail | Must still pass |
|---|---|---|
| remove `post.community.local_only or` | `test_a_post_in_a_local_only_community_is_403` | the other two |
| remove `post.community.private or` | `test_a_post_in_a_private_community_is_403` | the other two |
| remove `or post.status < POST_STATUS_PUBLISHED` | `test_an_unpublished_post_is_403` | the other two |

Restore after each, and **verify `git diff app/activitypub/routes.py` is empty**
before committing. Report each kill and whether it was an assertion failure or
a crash.

- [ ] **Step 3: Write the 401 for a post**

```python
def test_a_post_is_401_when_the_author_has_blocked_the_requesting_instance(app, db_session, monkeypatch):
    """`post_ap`'s copy of `comment_ap`'s 401 guard. The Instance row is created
    with the exact domain `requestor_domain()` will extract, because
    `find_instance_id` CREATES AND COMMITS a sparse Instance row for an unknown
    domain -- so an absent row would silently yield an id the author has not
    blocked and this test would measure the wrong branch.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    blocked = make_instance('blocked.example')
    make_instance_block(author, blocked)

    response = ap_get(app, f'/post/{post.id}',
                      user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401
```

- [ ] **Step 4: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 20 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ap_content_objects.py
git commit -m "test: isolate all three disjuncts of post_ap's visibility guard"
```

---

### Task 5: `post_ap`'s remote redirect and browser delegation

**Files:**
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

- [ ] **Step 1: Write the remote 301**

```python
def test_a_remote_post_redirects_to_its_origin(app, db_session, monkeypatch):
    """`post_ap`'s `else` on `if post.is_local():` -- a 301 to the post's own
    `ap_id`. `Post.is_local()` is `ap_id is None or
    ap_id.startswith(SERVER_URL)` (app/models.py), so a remote `ap_id` on a
    DIFFERENT host is what makes it false.

    This is an asymmetry, not just a branch: `comment_ap` has no `is_local()`
    check at all and re-serves a remote reply's JSON as though this instance
    were authoritative for it. Registered, not fixed.
    """
    _double_the_delegates(monkeypatch)
    site, instance = seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    author = make_user(instance, 'remoteposter')
    post = make_post(community, author, 'https://peer.example/objects/xyz')
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 301
    assert response.headers['Location'] == 'https://peer.example/objects/xyz'
```

- [ ] **Step 2: Write the browser delegation**

```python
def test_a_browser_request_for_a_post_delegates_to_show_post(app, db_session, monkeypatch):
    """`post_ap`'s outer else: `block_honey_pot()` then `show_post(...)`.

    Both are asserted. `block_honey_pot` running is not incidental -- it is a
    side effect on the non-ActivityPub path that the ActivityPub path does not
    have, and no other endpoint in this slice calls it.

    `sort` comes from `current_user.default_comment_sort or 'hot'` for a logged
    -in user and 'hot' for an anonymous one; the test client is anonymous, so
    'hot' is the value asserted.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = browser_get(app, f'/post/{post.id}')

    assert response.status_code == 200
    assert calls['block_honey_pot'] == [True]
    assert len(calls['show_post']) == 1
    args, kwargs = calls['show_post'][0]
    assert args == (post.id,)
    assert kwargs['sort'] == 'hot'
    assert kwargs['low_bandwidth'] is False
```

- [ ] **Step 3: Write the POST-method test**

```python
def test_a_post_request_to_a_post_url_never_takes_the_activitypub_path(app, db_session, monkeypatch):
    """`post_ap`'s route accepts POST -- `methods=['GET', 'HEAD', 'POST']` --
    but its ActivityPub branch requires GET or HEAD, so a POST with an
    ActivityPub Accept header falls to `show_post` regardless of the header.

    `post_ap` is the ONLY one of the four content-object endpoints whose route
    accepts POST; the other three are GET-only (`comment_ap` also allows HEAD).
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    with app.test_client() as client:
        response = client.post(f'/post/{post.id}', headers={'Accept': AP_ACCEPT})

    assert response.status_code == 200
    assert len(calls['show_post']) == 1
    assert calls['post_to_page'] == []
```

- [ ] **Step 4: Write the HEAD test**

```python
def test_a_head_request_for_a_post_returns_an_empty_activitypub_body(app, db_session, monkeypatch):
    """`post_ap`'s `else: post_data = []` for HEAD -- an empty LIST, jsonified,
    not an empty body. `post_to_page` is NOT called, which is the observable
    difference and is asserted rather than inferred from the body.

    This branch is REACHABLE here because `post_ap`'s route lists HEAD.
    `post_replies_ap` and `post_ap_context` contain the same branch on
    GET-only routes, where it is dead code -- registered, not fixed.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    with app.test_client() as client:
        response = client.head(f'/post/{post.id}', headers={'Accept': AP_ACCEPT})

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert calls['post_to_page'] == []
```

- [ ] **Step 5: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 24 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_ap_content_objects.py
git commit -m "test: cover post_ap's remote redirect, browser delegation and method branches"
```

---

### Task 6: `post_replies_ap` — pin the crash and cover the ActivityPub path

**Files:**
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: `test_a_browser_request_for_post_replies_crashes`, which **Task 10 inverts**. Task 10 needs this exact name.

**DO NOT FIX ANYTHING IN THIS TASK.** The crash is pinned here and fixed in
Task 10, in its own commit, so the fix has a witnessed pre-fix failure.

- [ ] **Step 1: Pin the crash**

```python
def test_a_browser_request_for_post_replies_crashes(app, db_session, monkeypatch):
    """PINS a crash, remotely reachable. DO NOT FIX -- a later task does.

    `post_replies_ap`'s entire body sits inside `if (request.method == 'GET' or
    request.method == 'HEAD') and is_activitypub_request():` and there is NO
    `else`. A browser request falls off the end, the view returns None, and
    Flask raises. `post_ap_context`, twelve lines below in the same file, gets
    this right with `else: abort(400)`.

    This is the FOURTH instance of the class sub-project 10 fixed three times
    in the feed collections (D167-D169).

    THE 500 DOES NOT MATERIALISE AS A RESPONSE. `tests/conftest.py` sets
    `TESTING = True` with no `PROPAGATE_EXCEPTIONS` override, so Flask
    re-raises rather than producing a 500, and the test client's default
    `raise_server_exceptions=True` lets it escape `browser_get` entirely
    (harness fact 43). So this asserts the exception, not a status code.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    with pytest.raises(TypeError, match='did not return a valid response'):
        browser_get(app, f'/post/{post.id}/replies')
```

- [ ] **Step 2: Run it and confirm it passes**

Run: `./run_tests.sh tests/test_ap_content_objects.py::test_a_browser_request_for_post_replies_crashes -q`
Expected: PASS. **If it does not raise `TypeError`, report exactly what it does
instead and stop** — the brief's claim would then be wrong and Task 10 depends
on it.

- [ ] **Step 3: Write the ActivityPub happy path**

```python
def test_post_replies_are_served_as_an_ordered_collection(app, db_session, monkeypatch):
    """`post_replies_ap`'s only working path. `totalItems` is `len(replies)`
    from the doubled `post_replies_for_ap`, and `Cache-Control` is 15 --
    against `post_ap`'s and `comment_ap`'s 120, for the same content.
    """
    calls = _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = ap_get(app, f'/post/{post.id}/replies')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=15'
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['totalItems'] == 1
    assert '@context' in response.json
    assert calls['post_replies_for_ap'] == [post.id]
```

- [ ] **Step 4: Pin the missing visibility guards**

These three tests pin **defects**, not correct behaviour. Each documents that
`post_replies_ap` serves what `post_ap` refuses for the same post.

```python
def test_post_replies_are_served_for_a_local_only_community(app, db_session, monkeypatch):
    """PINS a defect. `post_ap` aborts 403 for a `local_only` community;
    `post_replies_ap` has no visibility guard at all, so the same post's
    replies are enumerated to any caller. `local_only` is set explicitly; it
    defaults to False.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    community.local_only = True
    community.private = False
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/replies')

    assert response.status_code == 200
    assert response.json['totalItems'] == 1


def test_post_replies_are_served_for_an_unpublished_post(app, db_session, monkeypatch):
    """PINS a defect. `post_ap` aborts 403 on `status < POST_STATUS_PUBLISHED`;
    `post_replies_ap` applies no status guard, so an under-review post's
    replies are published. Status is set explicitly -- the column default is
    POST_STATUS_PUBLISHED, so leaving it implicit would assert nothing.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.status = POST_STATUS_REVIEWING
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/replies')

    assert response.status_code == 200


def test_post_replies_are_served_for_a_deleted_post(app, db_session, monkeypatch):
    """PINS a defect, and it is the sharpest of the three: `post_ap_context`
    -- the endpoint immediately BELOW this one, serving the same post's reply
    URIs -- aborts 404 on `post.deleted`. `post_replies_ap` does not.

    `deleted` is set explicitly; `make_post` sets `deleted=False`, so this is
    a contrary baseline rather than a default.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.deleted = True
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/replies')

    assert response.status_code == 200
```

- [ ] **Step 5: Write the unknown-post 404**

```python
def test_an_unknown_post_replies_collection_is_404(app, db_session, monkeypatch):
    """`Post.query.get_or_404` inside the ActivityPub branch. Reached only with
    an ActivityPub Accept header -- a browser request for the same URL crashes
    before the lookup, which is what
    `test_a_browser_request_for_post_replies_crashes` pins.
    """
    _double_the_delegates(monkeypatch)
    seed_actors()

    response = ap_get(app, '/post/999999/replies')

    assert response.status_code == 404
```

- [ ] **Step 6: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 30 passed.

- [ ] **Step 7: Commit**

```bash
git add tests/test_ap_content_objects.py
git commit -m "test: pin post_replies_ap's crash and its missing visibility guards"
```

---

### Task 7: `post_ap_context`

**Files:**
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: Task 1's helpers.
- Produces: nothing later tasks need.

`post_ap_context` builds its collection from a **real query**, not a doubled
delegate: `PostReply.query.filter_by(post_id=post_id, deleted=False)`. So its
tests seed real `PostReply` rows.

- [ ] **Step 1: Write the happy path**

```python
def test_a_post_context_lists_the_post_and_its_replies(app, db_session, monkeypatch):
    """`post_ap_context` builds its own collection from a real query rather
    than a delegate. `orderedItems` is `[post.ap_id] + [reply.ap_id ...]`, so
    the post's own URI comes FIRST and `totalItems` counts it.

    Both `ap_id`s are set explicitly. `make_post(..., None)` and
    `make_post_reply` both leave `ap_id` None for a local object, and asserting
    a list of Nones would be vacuous (harness fact 50) -- it would pass equally
    against a route that rendered `public_url()` or nothing at all.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.ap_id = 'https://test.piefed.local/post/1'
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://test.piefed.local/comment/1'
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/context')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=15'
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['totalItems'] == 2
    assert response.json['orderedItems'] == ['https://test.piefed.local/post/1',
                                             'https://test.piefed.local/comment/1']
    assert response.json['name'] == 'a post'
```

**Note:** setting `post.ap_id` to a `test.piefed.local` URL keeps
`Post.is_local()` true, which does not matter to this endpoint (it has no
`is_local` check) but keeps the fixture honest about what it represents.

- [ ] **Step 2: Write the metadata assertions**

```python
def test_a_post_context_attributes_itself_to_the_community(app, db_session, monkeypatch):
    """`attributedTo` and `audience` are BOTH `post.community.profile_id()`,
    and `id` is `post.public_url() + '/context'`. Asserted together because
    all three come from the post's relationships rather than from the request,
    and a regression swapping community for author would be invisible in the
    happy-path test above.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.ap_id = 'https://test.piefed.local/post/1'
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/context')

    assert response.status_code == 200
    assert response.json['attributedTo'] == community.profile_id()
    assert response.json['audience'] == community.profile_id()
    assert response.json['id'] == f'{post.public_url()}/context'
    assert response.json['attributedTo'] != post.public_url()
```

**If `profile_id()` or `public_url()` returns `None` for these fixtures**, that
is a finding, not a reason to weaken the assertion — report it and set whatever
column they read so the values are real strings.

- [ ] **Step 3: Write the deleted-post 404 and the deleted-reply filter**

```python
def test_a_deleted_post_has_no_context(app, db_session, monkeypatch):
    """`if post.deleted: abort(404)` -- the ONLY `deleted` guard among the four
    content-object endpoints. `post_replies_ap`, which serves the same post's
    replies, has none, which is pinned by
    `test_post_replies_are_served_for_a_deleted_post`.

    `deleted` is set explicitly; `make_post` sets `deleted=False`.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.deleted = True
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/context')

    assert response.status_code == 404


def test_a_post_context_omits_deleted_replies(app, db_session, monkeypatch):
    """The query's `deleted=False` filter. Two replies are seeded and one is
    deleted, so `totalItems` of 2 (the post plus one surviving reply) rather
    than 3 is what proves the filter ran -- a single-reply fixture could not
    tell a working filter from a missing one.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    post.ap_id = 'https://test.piefed.local/post/1'
    kept = make_post_reply(post, author, body='kept')
    kept.ap_id = 'https://test.piefed.local/comment/kept'
    gone = make_post_reply(post, author, body='gone')
    gone.ap_id = 'https://test.piefed.local/comment/gone'
    gone.deleted = True
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/context')

    assert response.status_code == 200
    assert response.json['totalItems'] == 2
    assert 'https://test.piefed.local/comment/gone' not in response.json['orderedItems']
```

- [ ] **Step 4: Mutation-test the `deleted=False` filter**

Remove `deleted=False` from `PostReply.query.filter_by(post_id=post_id,
deleted=False)` and confirm `test_a_post_context_omits_deleted_replies` fails
while every other test passes. Then remove `post_id=post_id` instead and
confirm the same test fails for the other reason. Restore and verify the diff
is empty. Report both kills.

- [ ] **Step 5: Write the non-ActivityPub 400**

```python
def test_a_browser_request_for_a_post_context_is_400(app, db_session, monkeypatch):
    """`post_ap_context`'s `else: abort(400)` -- the shape `post_replies_ap`
    is missing entirely. This is the model a later task copies.

    400 rather than 404: the resource exists, the request is simply not an
    ActivityPub one.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = browser_get(app, f'/post/{post.id}/context')

    assert response.status_code == 400
```

- [ ] **Step 6: Write the unknown-post 404**

```python
def test_an_unknown_post_context_is_404(app, db_session, monkeypatch):
    """`Post.query.get_or_404`, reached before the `deleted` check."""
    _double_the_delegates(monkeypatch)
    seed_actors()

    response = ap_get(app, '/post/999999/context')

    assert response.status_code == 404
```

- [ ] **Step 7: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 36 passed.

- [ ] **Step 8: Commit**

```bash
git add tests/test_ap_content_objects.py
git commit -m "test: cover post_ap_context's collection, deleted guards and 400"
```

---

### Task 8: `activities_json`

**Files:**
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: Task 1's helpers and `make_activitypub_log`.
- Produces: nothing later tasks need.

`activities_json` matches on the **full URI** it builds:
`f"{current_app.config['SERVER_URL']}/activities/{type}/{id}"`. So
`make_activitypub_log` must be given that exact string.

- [ ] **Step 1: Write the found-with-json path**

```python
def test_a_logged_activity_is_served_as_its_stored_json(app, db_session):
    """`activities_json` matches `ActivityPubLog.activity_id` against the FULL
    URI it builds from SERVER_URL and the two path segments, not against a bare
    id -- so the seeded row carries the whole URI.

    `activity_json` is stored as a TEXT column holding a JSON string and the
    route calls `json.loads` on it, so the factory is given a string and the
    assertion reads back a dict.

    `@cache.cached(timeout=2400)` on this route is inert under test
    (CACHE_TYPE='NullCache'), so this measures the view, not the cache.
    """
    seed_actors()
    make_activitypub_log('https://test.piefed.local/activities/announce/abc123',
                         activity_type='Announce',
                         activity_json='{"type": "Announce", "id": "https://test.piefed.local/activities/announce/abc123"}')

    with app.test_client() as client:
        response = client.get('/activities/announce/abc123')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=2400'
    assert response.json['type'] == 'Announce'
```

**`SERVER_URL` is derived, not configured.** `tests/conftest.py` sets
`SERVER_NAME = 'test.piefed.local'` and `create_app` builds
`SERVER_URL` from it plus `HTTP_PROTOCOL` (`app/__init__.py:131-134`), so
confirm the value at runtime rather than trusting this plan — the match is
exact, and a wrong host makes this test measure the NOT-FOUND branch while
still passing a `status_code` assertion, which is precisely the failure this
note exists to prevent. If it differs, use the real value; do not weaken the
assertion to a substring match.

- [ ] **Step 2: Write the null-json branch**

```python
def test_a_logged_activity_with_no_json_serves_an_empty_document(app, db_session):
    """`if activity.activity_json is not None:` -- the else sets
    `activity_json = {}`. The column is nullable and `make_activitypub_log`
    defaults it to None, so this test passes `activity_json=None` EXPLICITLY
    to state which branch it means rather than relying on the factory default.
    """
    seed_actors()
    make_activitypub_log('https://test.piefed.local/activities/announce/nojson',
                         activity_json=None)

    with app.test_client() as client:
        response = client.get('/activities/announce/nojson')

    assert response.status_code == 200
    assert response.json == {}
```

- [ ] **Step 3: Write the not-found branch**

```python
def test_an_unlogged_activity_is_404_with_a_cache_header(app, db_session):
    """`else: resp = make_response('', 404)`. The `Cache-Control` header is set
    AFTER the if/else, so the 404 carries it too -- asserted because a
    2400-second cache on a 404 is a real behaviour and not obviously intended.
    """
    seed_actors()

    with app.test_client() as client:
        response = client.get('/activities/announce/missing')

    assert response.status_code == 404
    assert response.headers['Cache-Control'] == 'public, max-age=2400'
```

- [ ] **Step 4: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 39 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ap_content_objects.py
git commit -m "test: cover activities_json's three branches"
```

---

### Task 9: `activity_result`, including the exception-message disclosure

**Files:**
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: Task 1's helpers and `make_activitypub_log`.
- Produces: nothing later tasks need.

`activity_result` matches `f'https://{id}'` where `id` is a `<path:id>` route
parameter, so the URL `/activity_result/peer.example/activities/announce/abc`
matches the stored `activity_id` `https://peer.example/activities/announce/abc`.

**DO NOT FIX the disclosure.** It is registered in Task 11 as this slice's most
serious finding. Pin it.

- [ ] **Step 1: Write the success branch**

```python
def test_a_successful_activity_result_is_ok(app, db_session):
    """`activity_result` matches `f'https://{id}'` where `id` is a <path:id>
    parameter, so the multi-segment path in the URL becomes the host and path
    of the stored `activity_id`.

    `result='success'` is passed explicitly even though it is the factory's
    default, because the assertion is ABOUT that value -- its twin below sets
    'failure' and gets a different document.
    """
    seed_actors()
    make_activitypub_log('https://peer.example/activities/announce/abc',
                         result='success')

    with app.test_client() as client:
        response = client.get('/activity_result/peer.example/activities/announce/abc')

    assert response.status_code == 200
    assert response.json == 'Ok'
```

- [ ] **Step 2: Pin the exception-message disclosure**

```python
def test_a_failed_activity_result_discloses_the_internal_exception_message(app, db_session):
    """PINS A DEFECT, and it is this slice's most serious. DO NOT FIX --
    registered, because choosing the replacement is a decision about what peers
    are told.

    On a non-'success' result the endpoint returns
    `{'error': activity.result, 'message': activity.exception_message}`.
    `ActivityPubLog.exception_message` is populated from caught exceptions, so
    this instance's internal error text is served to anyone who can name an
    activity id -- and the id is one the REMOTE instance chose and therefore
    already knows. There is no authentication on this route.

    The message asserted here is deliberately shaped like a real internal
    error, including a file path, to make the disclosure legible in the test
    output rather than abstract.
    """
    seed_actors()
    make_activitypub_log('https://peer.example/activities/announce/boom',
                         result='failure',
                         exception_message="IntegrityError at app/activitypub/util.py:1214: duplicate key value violates unique constraint \"user_ap_id_key\"")

    with app.test_client() as client:
        response = client.get('/activity_result/peer.example/activities/announce/boom')

    assert response.status_code == 200
    assert response.json['error'] == 'failure'
    assert 'app/activitypub/util.py:1214' in response.json['message']
    assert 'user_ap_id_key' in response.json['message']
```

- [ ] **Step 3: Write the not-found branch**

```python
def test_an_unknown_activity_result_is_404(app, db_session):
    """`else: abort(404)`. `activity_result` gets this right, which is worth
    recording: `post_replies_ap` -- in the same file, covered by this same
    suite -- has no else at all and crashes instead.
    """
    seed_actors()

    with app.test_client() as client:
        response = client.get('/activity_result/peer.example/activities/announce/nope')

    assert response.status_code == 404
```

- [ ] **Step 4: Mutation-test the result branch**

Change `if activity.result == 'success':` to `if activity.result != 'success':`
and confirm **both** `test_a_successful_activity_result_is_ok` and
`test_a_failed_activity_result_discloses_the_internal_exception_message` fail —
this is a case where a multi-kill is correct and expected, because the mutation
swaps two branches that are both covered. Report it as a multi-kill, not as a
problem. Restore and verify the diff is empty.

- [ ] **Step 5: Run the tests**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: 42 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_ap_content_objects.py
git commit -m "test: cover activity_result and pin its exception-message disclosure"
```

---

### Task 10: Fix `post_replies_ap`'s missing `else`

**Files:**
- Modify: `app/activitypub/routes.py`
- Modify: `tests/test_ap_content_objects.py`

**Interfaces:**
- Consumes: `test_a_browser_request_for_post_replies_crashes` from Task 6.
- Produces: nothing later tasks need.

**This is the only task in the sub-project that changes production code.**

The fix is `else: abort(400)`, matching `post_ap_context` — the sibling twelve
lines below that gets this right. **Match it rather than inventing a fifth
spelling.** Do not adopt `comment_ap`'s or `post_ap`'s HTML-delegation answer:
there is no HTML view for a replies collection, and choosing one is a larger
change than this slice authorises.

**DO NOT** fix `post_replies_ap`'s missing visibility guards, the dead `HEAD`
branches, or `activity_result`'s disclosure. Their pins stay exactly as they are.

- [ ] **Step 1: Invert the pin**

Rename `test_a_browser_request_for_post_replies_crashes` to
`test_a_browser_request_for_post_replies_is_400`, replace the
`pytest.raises` block with a plain call plus a status assertion, and rewrite
the docstring so it no longer describes a live crash:

```python
def test_a_browser_request_for_post_replies_is_400(app, db_session, monkeypatch):
    """`post_replies_ap`'s `else: abort(400)`, added because the function
    previously had no `else` at all: a browser request fell off the end, the
    view returned None, and Flask raised
    `TypeError: The view function ... did not return a valid response`.

    400 rather than 404 matches `post_ap_context`, the sibling twelve lines
    below, which had the correct shape all along. `comment_ap` and `post_ap`
    answer a browser with HTML instead, which is a richer answer this fix
    deliberately did not adopt -- there is no HTML view for a replies
    collection.
    """
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()

    response = browser_get(app, f'/post/{post.id}/replies')

    assert response.status_code == 400
```

Remove the `import pytest` at the top of the file **only if** no other test
still uses it — grep first.

- [ ] **Step 2: Run it and watch it FAIL**

Run: `./run_tests.sh tests/test_ap_content_objects.py::test_a_browser_request_for_post_replies_is_400 -q`
Expected: FAIL, with the `TypeError` escaping rather than a 400 arriving.
**Quote the failure text verbatim in your report** — that output is the proof
the test discriminates.

- [ ] **Step 3: Apply the fix**

Locate `post_replies_ap` by content and add the missing `else` at the end of
the function:

```python
        resp.headers.set('Cache-Control', 'public, max-age=15')
        return resp
    else:
        abort(400)
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `./run_tests.sh tests/test_ap_content_objects.py -q`
Expected: all tests pass.

- [ ] **Step 5: Mutation-prove the fix**

Remove the `else: abort(400)` again, confirm
`test_a_browser_request_for_post_replies_is_400` fails, restore. Report the
mutation, the test that died, and note that this is necessarily a **crash-kill,
not an assertion-kill**: the mutant raises before the status assertion is
reached, so an assertion-kill is structurally impossible for this shape
(harness fact 44).

- [ ] **Step 6: Audit docstrings falsified by the fix**

Every claim in `tests/test_ap_content_objects.py` that `post_replies_ap`
crashes is now false. Grep the file for `crash`, `raises`, `TypeError`, `500`
and `no else`, and check each hit. Pay particular attention to:

- `test_a_browser_request_for_a_comment_delegates_to_the_discussion_view`, whose
  docstring names the crash pin by its **old** name;
- `test_an_unknown_post_replies_collection_is_404`, which says a browser request
  "crashes before the lookup";
- `test_a_browser_request_for_a_post_context_is_400`, which calls itself "the
  model a later task copies";
- `test_an_unknown_activity_result_is_404`, which says `post_replies_ap` "has no
  else at all and crashes instead".

A prior sub-project lost two review rounds to exactly this class of miss.

- [ ] **Step 7: Check for a vacated branch**

Name the branch the inverted test used to cover and the test that covers it now.
If none does, add one and mutation-prove it.

- [ ] **Step 8: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_content_objects.py
git commit -m "fix: return 400 for a non-ActivityPub request to a post's replies"
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

Read the SDD ledger at
`.superpowers/sdd/2026-09-03-coverage-content-objects-11/progress.md` in full —
it records every task's outcome, every reviewer verdict and every controller
ruling, including defects found but not authorised to fix. Then read the
per-task reports in the same directory, and the spec. **Register what was
found, not what was predicted**; where a prediction was falsified, the
falsification is itself worth recording.

- [ ] **Step 2: Write the register entries, numbered from D187**

Match the surrounding entries' house style — read several existing D-entries
first. Each entry states the defect, where it lives (by content, with a line
citation correct **at your commit**), how it was found, whether it was fixed or
registered, and if registered, **why not fixed**.

At minimum, and the ledger will have more:

1. **`activity_result` discloses internal exception messages** to any
   unauthenticated caller. Register this **first and as the most serious
   finding in the slice**, not buried in a list.
2. **`post_replies_ap`'s missing `else`** — FIXED, with its commit, recorded as
   the fourth instance of the class D167-D169 covered.
3. **`post_replies_ap` has no visibility guard at all** — no `deleted`, no
   `local_only`, no `private`, no `status`, where `post_ap` has all four.
4. **`post_ap_context` checks only `deleted`** — no `local_only`, `private` or
   `status`, so a local-only community's post titles and reply URIs are served.
5. **`comment_ap` never checks `reply.deleted`** and never checks `is_local()`,
   so it re-serves remote replies where `post_ap` 301-redirects.
6. **Two dead `HEAD` branches** on the GET-only `post_replies_ap` and
   `post_ap_context` routes.
7. **`find_instance_id()` writes and commits an `Instance` row from an
   unauthenticated GET**, reached from `comment_ap` and `post_ap`.
8. **The instance-block guard is inert without a `+`-style `User-Agent`.**
9. **Four different `Cache-Control` max-ages** across four documents of the same
   kind (120, 120, 15, 15), plus 2400 on `activities_json` — the same spread
   D180 registered for the collections.
10. **`activities_json` serves a 404 carrying `Cache-Control: max-age=2400`.**

- [ ] **Step 3: Update BOTH "Next free number" notes**

The register has **two**. Find every occurrence of that phrase; the two that
carry the campaign's current value must both be updated to the new next-free
number, and the historical ones left alone. A previous sub-project updated only
one and the register disagreed with itself.

- [ ] **Step 4: Add the harness facts to `tests/README.md`**

Read the file first and match its style; the numbered facts currently run to 50.
Add only what is not already there. Candidates:

- `requestor_domain()` returns `''` unless the `User-Agent` contains a `+`, so
  the instance-block guards in `comment_ap` and `post_ap` are unreachable
  without one — a test that omits it measures the wrong branch.
- `find_instance_id()` creates and commits a sparse `Instance` row for an
  unknown domain, so a test that sends a `+`-style agent for a domain it did not
  seed silently creates one and gets an id the author has not blocked.
- `has_blocked_instance(id)` is per-instance while `has_blocked_instances()` is
  a global flag, and the two drive different branches of the same endpoint.
- Anything else in the ledger's rulings that generalises.

- [ ] **Step 5: Raise the floor**

`coverage_floors.ini`, `app/activitypub/routes.py`: raise from 86 to the
measured blended figure rounded down. **The controller supplies that figure —
do not run a coverage run.**

- [ ] **Step 6: Verify every citation**

Task 10's fix shifted lines in `app/activitypub/routes.py`. Verify every
`app/*.py` line citation you wrote against the current source. Stale citations
are this campaign's most frequent defect.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 11's findings and raise the routes.py floor"
```

---

## Self-review

**Spec coverage.** Each of the spec's six functions has at least one task:
`comment_ap` Tasks 1-2, `post_ap` Tasks 3-5, `post_replies_ap` Tasks 6 and 10,
`post_ap_context` Task 7, `activities_json` Task 8, `activity_result` Task 9.
Every row of the spec's asymmetry table is reached: route methods (Task 5),
dead HEAD branches (Task 5 asserts the live one, Task 11 registers the dead
ones), `local_only`/`private` (Tasks 2 and 4), `status` (Task 4), `deleted`
(Tasks 6 and 7), instance-block (Tasks 2 and 4), remote object (Task 5),
non-AP request (Tasks 2, 5, 6, 7), `Cache-Control` (Tasks 1, 3, 6, 7, 8),
`Vary` (Tasks 1, 2, 3), `Link` (Tasks 1, 3). The spec's six success criteria map
to Tasks 1-9 (1-3), Task 10 (4), Task 11 (5-8) and the controller's final run (9).

**Placeholder scan.** No "TBD", no "add appropriate error handling", no "similar
to Task N". Every code step carries real code. Three steps deliberately say
"if X is not true, stop and report" — those are falsifiable checks against a
stated expectation, not placeholders.

**Type consistency.** `ap_get(app, path, user_agent=None)`, `browser_get(app,
path)`, `seed_local_post(community=None, user=None, title='a post')` and
`_double_the_delegates(monkeypatch)` are named identically in every task that
uses them. `make_activitypub_log`'s keyword names (`direction`, `activity_type`,
`activity_json`, `result`, `exception_message`) match between Task 1's
definition and Tasks 8-9's calls. `test_a_browser_request_for_post_replies_crashes`
is the name Task 6 produces and Task 10 consumes, and Task 10 states the new
name explicitly.

**One gap found and fixed inline:** Task 6's Step 4 originally pinned only the
`local_only` guard; the spec claims `post_replies_ap` has *no* guard at all, so
`status` and `deleted` pins were added to make that claim testable rather than
asserted.

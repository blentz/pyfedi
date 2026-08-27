# Mastodon Visibility Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse followers-only and direct Mastodon content at ingest, and remove the incorrect feed gate that currently makes every boosted post invisible.

**Architecture:** A pure classifier reads an ActivityPub object's `to`/`cc` and returns one of four visibility levels. `create_post` and `create_post_reply` refuse `followers` and `direct` with distinct logged reasons — one enforcement point per model, covering the Create, Announce and boost paths. Once refusal is in place, the `p.private is false` gate is removed from the boost feed clause, because it gates on a flag that means "titleless", not "private", and currently excludes every ingested Mastodon post.

**Tech Stack:** Python 3, Flask, SQLAlchemy 2.0, PostgreSQL, pytest, podman-compose.

**Spec:** `docs/superpowers/specs/2026-08-24-mastodon-visibility-mapping-design.md`

## Global Constraints

- **Ordering is mandatory: Task 2 (refusal) MUST land before Task 3 (gate removal).** Today that gate is the only thing keeping non-public boosted content out of the feed — it does so by excluding everything. Removing it first opens a real hole. Never leave the tree in a state where the gate is gone and refusal is absent.
- The classifier takes the **object**, never the wrapping activity. In the boost path `create_resolved_object` synthesises `{'id': ..., 'object': post_data}` with no addressing at the activity level; an activity-level read would see nothing and classify everything as public.
- `as:Public` has three legal spellings — `https://www.w3.org/ns/activitystreams#Public`, `as:Public`, and bare `Public`. All three must match. Recognising only the first classifies followers-only content from some implementations as public.
- `to` and `cc` are each independently a string, a list, or absent. Normalise before inspecting. An unguarded `obj['to'][0]` is how the existing reply heuristic ended up working only for single-element addressing.
- `Post.private` does NOT mean "not public" — `Post.new` sets it for any titleless object (`app/models.py:1796-1797`), i.e. every microblog post. It is an unlisted marker. `PostReply.private` DOES mean followers-only (`app/models.py:2827-2830`). Do not conflate them.
- No database migration. No new column. No new runtime dependencies.
- Direct messages must keep working: `process_chat` handles them at `app/activitypub/routes.py:1169` and `:1191`, before the Create dispatch reaches `create_post`.
- Tests run only via `./run_tests.sh [pytest args]`. This repo has NO host Python environment — never run `pytest`, `python`, or `flask` on the host. See `tests/README.md`.
- Baseline before starting: `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py` → 234 passed, 0 failed. Must still be 0 failed at every commit.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `app/activitypub/util.py` | `activitypub_visibility()` and its `_addressing_list()` helper; refusal in `create_post` and `create_post_reply` | Modify |
| `app/utils.py` | Remove the `p.private is false` gate from the boost disjunct | Modify |
| `tests/factories.py` | `make_post` gains a `microblog` mode; correct the misleading `private` docstring | Modify |
| `tests/test_visibility.py` | Pure classifier tests | Create |
| `tests/test_visibility_ingest.py` | Refusal tests for posts and replies, plus the DM regression | Create |
| `tests/test_feed_boost_visibility.py` | Re-point at production-shaped posts | Modify |

---

## Task 1: The visibility classifier

**Files:**
- Modify: `app/activitypub/util.py` — add both functions immediately above `announce_target_uri` (currently line 3487)
- Test: `tests/test_visibility.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `activitypub_visibility(obj: dict) -> str` returning `'public'`, `'unlisted'`, `'followers'`, or `'direct'`
  - `_addressing_list(obj: dict, field: str) -> list` — normalises one addressing field

Pure functions: no database, no network, no app context.

- [ ] **Step 1: Write the failing test**

Create `tests/test_visibility.py`:

```python
import unittest

from app.activitypub.util import activitypub_visibility

PUBLIC_SPELLINGS = [
    'https://www.w3.org/ns/activitystreams#Public',
    'as:Public',
    'Public',
]
FOLLOWERS = 'https://m.example/users/alice/followers'


class TestActivityPubVisibility(unittest.TestCase):

    def test_public_in_to_all_spellings(self):
        """Every legal spelling of as:Public in `to` means public"""
        for spelling in PUBLIC_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    activitypub_visibility({'to': [spelling], 'cc': [FOLLOWERS]}), 'public')

    def test_unlisted_all_spellings(self):
        """Public in `cc` but not `to` means unlisted"""
        for spelling in PUBLIC_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    activitypub_visibility({'to': [FOLLOWERS], 'cc': [spelling]}), 'unlisted')

    def test_followers_only(self):
        """A followers URL addressed with no Public anywhere means followers-only"""
        self.assertEqual(activitypub_visibility({'to': [FOLLOWERS], 'cc': []}), 'followers')

    def test_followers_url_in_cc_only(self):
        """A followers URL in cc still counts as followers-only"""
        self.assertEqual(activitypub_visibility({'to': [], 'cc': [FOLLOWERS]}), 'followers')

    def test_direct(self):
        """Addressed only to named actors means direct"""
        self.assertEqual(
            activitypub_visibility({'to': ['https://m.example/users/bob'], 'cc': []}), 'direct')

    def test_to_as_bare_string(self):
        """`to` may be a string rather than a list"""
        self.assertEqual(
            activitypub_visibility({'to': 'https://www.w3.org/ns/activitystreams#Public'}), 'public')

    def test_cc_as_bare_string(self):
        """`cc` may be a string rather than a list"""
        self.assertEqual(
            activitypub_visibility({'to': FOLLOWERS, 'cc': 'as:Public'}), 'unlisted')

    def test_both_absent(self):
        """An object with no addressing at all is direct, not public"""
        self.assertEqual(activitypub_visibility({'type': 'Note'}), 'direct')

    def test_non_dict(self):
        """Malformed input is direct, the safe default"""
        self.assertEqual(activitypub_visibility(None), 'direct')

    def test_non_string_entries_ignored(self):
        """Non-string entries in an addressing list do not raise"""
        self.assertEqual(
            activitypub_visibility({'to': [None, 123, 'as:Public']}), 'public')

    def test_public_wins_over_followers_in_to(self):
        """Public and a followers URL together in `to` is public"""
        self.assertEqual(
            activitypub_visibility({'to': ['as:Public', FOLLOWERS]}), 'public')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_visibility.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'activitypub_visibility' from 'app.activitypub.util'`

- [ ] **Step 3: Write minimal implementation**

In `app/activitypub/util.py`, immediately above `def announce_target_uri`:

```python
AS_PUBLIC = ('https://www.w3.org/ns/activitystreams#Public', 'as:Public', 'Public')


def _addressing_list(obj: dict, field: str) -> list:
    """Normalise one ActivityPub addressing field to a list of strings.

    `to` and `cc` are each independently a string, a list, or absent.
    """
    if not isinstance(obj, dict):
        return []
    value = obj.get(field)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, str)]
    return []


def activitypub_visibility(obj: dict) -> str:
    """Classify an object's audience as 'public', 'unlisted', 'followers' or 'direct'.

    Pass the OBJECT, never the wrapping activity. In the boost path
    create_resolved_object() synthesises {'id': ..., 'object': post_data} with no
    addressing at the activity level, so reading the activity would see nothing and
    classify everything as public.

    Note that Post.private does NOT mean "not public": Post.new() sets it for any
    titleless object, i.e. every microblog post, making it an unlisted marker.
    PostReply.private does mean followers-only. The two are not the same thing.
    """
    to = _addressing_list(obj, 'to')
    cc = _addressing_list(obj, 'cc')

    if any(addr in AS_PUBLIC for addr in to):
        return 'public'
    if any(addr in AS_PUBLIC for addr in cc):
        return 'unlisted'
    if any(addr.endswith('/followers') for addr in to + cc):
        return 'followers'
    return 'direct'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_visibility.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add tests/test_visibility.py app/activitypub/util.py
git commit -m "feat: add ActivityPub visibility classifier"
```

---

## Task 2: Refuse followers-only and direct content at ingest

**Files:**
- Modify: `app/activitypub/util.py` — `create_post` (currently line 2487) and `create_post_reply` (currently line 2311)
- Test: `tests/test_visibility_ingest.py`

**Interfaces:**
- Consumes: `activitypub_visibility(obj)` from Task 1.
- Produces: no new symbols. `create_post` and `create_post_reply` keep their existing signatures and return `None` for refused content.

Both functions sit downstream of the Create path, the Announce path, and the boost path, so one check per model covers all three. Enforcing further up would mean three copies of a security check, and duplicated security checks rot.

- [ ] **Step 1: Write the failing test**

Create `tests/test_visibility_ingest.py`:

```python
import pytest

from tests.factories import make_community, make_instance, make_post, make_user

FOLLOWERS = 'https://m.example/users/alice/followers'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@pytest.fixture
def author(db_session):
    return make_user(make_instance('m.example'), 'alice')


@pytest.fixture
def log_spy(monkeypatch):
    """Capture log_incoming_ap calls as (aplog_type, aplog_result, message)."""
    calls = []

    def fake_log(id, aplog_type, aplog_result, saved_json, message=None, session=None):
        calls.append((aplog_type, aplog_result, message))

    monkeypatch.setattr('app.activitypub.util.log_incoming_ap', fake_log)
    return calls


def note_activity(visibility_to, visibility_cc):
    return {
        'id': 'https://m.example/users/alice/statuses/1/activity',
        'type': 'Create',
        'object': {
            'id': 'https://m.example/users/alice/statuses/1',
            'type': 'Note',
            'content': '<p>hello</p>',
            'attributedTo': 'https://m.example/users/alice',
            'to': visibility_to,
            'cc': visibility_cc,
        },
    }


def test_followers_only_post_is_refused(db_session, author, log_spy):
    """A followers-only post is not stored"""
    from app.activitypub.util import create_post
    from app.models import Post
    community = make_community()

    result = create_post(False, community, note_activity([FOLLOWERS], []), author)

    assert result is None
    assert Post.query.count() == 0
    assert 'followers' in log_spy[-1][2]


def test_direct_post_is_refused(db_session, author, log_spy):
    """A direct post is not stored"""
    from app.activitypub.util import create_post
    from app.models import Post
    community = make_community()

    result = create_post(False, community, note_activity(['https://m.example/users/bob'], []), author)

    assert result is None
    assert Post.query.count() == 0
    assert 'direct' in log_spy[-1][2]


def test_public_post_is_accepted(db_session, author, log_spy):
    """A public post is stored"""
    from app.activitypub.util import create_post
    from app.models import Post
    community = make_community()

    result = create_post(False, community, note_activity([PUBLIC], [FOLLOWERS]), author)

    assert result is not None
    assert Post.query.count() == 1


def test_unlisted_post_is_accepted(db_session, author, log_spy):
    """An unlisted post is stored"""
    from app.activitypub.util import create_post
    from app.models import Post
    community = make_community()

    result = create_post(False, community, note_activity([FOLLOWERS], [PUBLIC]), author)

    assert result is not None
    assert Post.query.count() == 1


def test_followers_only_reply_is_refused(db_session, author, log_spy):
    """A followers-only reply is not stored"""
    from app.activitypub.util import create_post_reply
    from app.models import PostReply
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')

    activity = note_activity([FOLLOWERS], [])
    activity['object']['inReplyTo'] = parent.ap_id

    result = create_post_reply(False, community, parent.ap_id, activity, author)

    assert result is None
    assert PostReply.query.count() == 0
    assert 'followers' in log_spy[-1][2]


def test_direct_reply_is_refused(db_session, author, log_spy):
    """A direct reply is not stored"""
    from app.activitypub.util import create_post_reply
    from app.models import PostReply
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')

    activity = note_activity(['https://m.example/users/bob'], [])
    activity['object']['inReplyTo'] = parent.ap_id

    result = create_post_reply(False, community, parent.ap_id, activity, author)

    assert result is None
    assert PostReply.query.count() == 0
    assert 'direct' in log_spy[-1][2]


def test_post_and_reply_refusal_reasons_differ(db_session, author, log_spy):
    """Refusal reasons distinguish posts from replies, so logs are diagnosable"""
    from app.activitypub.util import create_post, create_post_reply
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')

    create_post(False, community, note_activity([FOLLOWERS], []), author)
    post_reason = log_spy[-1][2]

    activity = note_activity([FOLLOWERS], [])
    activity['object']['inReplyTo'] = parent.ap_id
    create_post_reply(False, community, parent.ap_id, activity, author)
    reply_reason = log_spy[-1][2]

    assert post_reason != reply_reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_visibility_ingest.py -v`
Expected: the four refusal tests FAIL — content is currently stored, so `result is None` and the row-count assertions fail. The two acceptance tests should already pass.

- [ ] **Step 3: Add refusal to create_post**

In `app/activitypub/util.py`, in `create_post`, immediately after the `community.local_only` check and before the `try:`:

```python
    visibility = activitypub_visibility(request_json.get('object'))
    if visibility in ('followers', 'direct'):
        log_incoming_ap(id, APLOG_CREATE, APLOG_IGNORED, saved_json,
                        f'Non-public post refused: {visibility}')
        return None
```

- [ ] **Step 4: Add refusal to create_post_reply**

In `app/activitypub/util.py`, in `create_post_reply`, immediately after the `community.local_only` check and before `find_reply_parent`:

```python
    visibility = activitypub_visibility(request_json.get('object'))
    if visibility in ('followers', 'direct'):
        log_incoming_ap(id, APLOG_CREATE, APLOG_IGNORED, saved_json,
                        f'Non-public reply refused: {visibility}')
        return None
```

The reason strings differ deliberately: an operator reading the log must be able to tell which model refused. `APLOG_IGNORED` and `APLOG_CREATE` arrive via `from app.constants import *`.

- [ ] **Step 5: Run test to verify it passes**

Run: `./run_tests.sh tests/test_visibility_ingest.py -v`
Expected: PASS, 7 tests

- [ ] **Step 6: Verify direct messages still work**

The refusal above rejects `direct` content, and DMs are direct. Confirm they never reach `create_post`: `process_chat` is called at `app/activitypub/routes.py:1169` and `:1191`, ahead of the Create dispatch.

Add to `tests/test_visibility_ingest.py`:

```python
def test_direct_message_path_is_unaffected(db_session, author):
    """DMs are handled by process_chat before create_post, so refusal cannot break them.

    Guards the ordering: if a future change moves the Create dispatch above
    process_chat, direct messages would start being refused instead of delivered.
    """
    import inspect
    from app.activitypub import routes

    source = inspect.getsource(routes.process_inbox_request)
    chat_at = source.index('process_chat')
    content_at = source.index('process_new_content(user')
    assert chat_at < content_at, 'process_chat must be reached before process_new_content'
```

Anchor on `process_new_content(user` (currently `app/activitypub/routes.py:1203`), which is the dispatch that reaches `create_post`. Do NOT anchor on `core_activity['object']['type']` — that string first appears at line 1054, above `process_chat` at 1169, so the assertion would be inverted and fail against correct code.

Run: `./run_tests.sh tests/test_visibility_ingest.py -v`
Expected: PASS, 8 tests

- [ ] **Step 7: Run the full suite**

Run: `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py`
Expected: 0 failed. The count rises from 234 by the number of tests added.

- [ ] **Step 8: Commit**

```bash
git add tests/test_visibility_ingest.py app/activitypub/util.py
git commit -m "feat: refuse followers-only and direct content at ingest"
```

---

## Task 3: Remove the incorrect feed gate

**Files:**
- Modify: `app/utils.py:3273-3277` — the boost disjunct
- Modify: `tests/factories.py:56-59` — `make_post` microblog mode and docstring
- Modify: `tests/test_feed_boost_visibility.py`

**Interfaces:**
- Consumes: refusal from Task 2 — this task is only safe once that has landed.
- Produces: `make_post(community, user, ap_id, title='a post', private=False, microblog=False)`.

**Do not start this task until Task 2 is committed.** The gate being removed is currently the only thing keeping non-public boosted content out of the feed.

- [ ] **Step 1: Give the factory a production-shaped mode**

The current docstring in `tests/factories.py:57-59` states the misconception that caused this bug — that `private=True` marks a followers-only post. Replace the signature and docstring:

```python
def make_post(community, user, ap_id: str, title: str = 'a post', private: bool = False,
              microblog: bool = False) -> Post:
    """Build a Post.

    microblog=True produces the shape ingestion actually creates for a Mastodon
    Note: no title, and private=True. Post.new() sets private for ANY titleless
    object (app/models.py:1796-1797), so Post.private is an unlisted marker, NOT a
    followers-only flag -- it is the filter on the discovery surfaces (search, tags,
    domains, community listings, profiles) while the subscribed feed skips it.
    PostReply.private is the one that means followers-only.

    Prefer microblog=True in any test about feed visibility of ingested content.
    Passing private= directly sets the column without the rest of the shape.
    """
    if microblog:
        private = True
        title = ''
```

Keep the rest of the function body unchanged, with `private=private` and `title=title` still passed to `Post(...)`.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_feed_boost_visibility.py`:

```python
def test_boosted_microblog_post_is_visible(db_session):
    """A boosted post in the shape ingestion really creates appears in the feed.

    Regression test: the feed clause gated on p.private is false, but Post.new sets
    private for every titleless object, so every ingested Mastodon post was excluded
    and boosted posts never appeared.
    """
    from app import db
    from app.activitypub.util import record_boost
    from sqlalchemy import text

    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    stranger = make_user(make_instance('other.example'), 'stranger')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    post = make_post(make_community(), stranger, 'https://other.example/notes/1', microblog=True)
    record_boost(post, booster)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id})]

    assert post.id in ids
```

- [ ] **Step 3: Update the clause constant the tests assert against**

In `tests/test_feed_boost_visibility.py`, change `BOOST_CLAUSE` to drop the privacy gate:

```python
BOOST_CLAUSE = """SELECT p.id FROM "post" as p WHERE
EXISTS (SELECT 1 FROM post_boost pb
        INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
        WHERE pb.post_id = p.id
        AND uf2.local_user_id = :local_user_id
        AND uf2.is_inward is false)"""
```

Delete `test_private_post_boosted_by_followed_account_is_not_visible` if present — it asserted behaviour this task deliberately removes, and the protection it stood for now lives in Task 2's refusal. Note the deletion in the commit message.

- [ ] **Step 4: Run test to verify it fails**

Run: `./run_tests.sh tests/test_feed_boost_visibility.py -v`
Expected: `test_clause_matches_the_one_in_utils` FAILS, because `app/utils.py` still contains `p.private is false` in the boost disjunct while `BOOST_CLAUSE` no longer does.

- [ ] **Step 5: Remove the gate**

In `app/utils.py`, replace lines 3265-3269:

```python
        sources.append("""(p.private is false AND EXISTS (SELECT 1 FROM post_boost pb
                                  INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
                                  WHERE pb.post_id = p.id
                                  AND uf2.local_user_id = :local_user_id
                                  AND uf2.is_inward is false))""")
```

with:

```python
        sources.append("""EXISTS (SELECT 1 FROM post_boost pb
                                  INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
                                  WHERE pb.post_id = p.id
                                  AND uf2.local_user_id = :local_user_id
                                  AND uf2.is_inward is false)""")
```

Non-public content is kept out by refusal at ingest (Task 2), not by this clause.

- [ ] **Step 6: Run test to verify it passes**

Run: `./run_tests.sh tests/test_feed_boost_visibility.py -v`
Expected: PASS, including `test_boosted_microblog_post_is_visible`

- [ ] **Step 7: Run the full suite**

Run: `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py`
Expected: 0 failed.

- [ ] **Step 8: Commit**

```bash
git add app/utils.py tests/factories.py tests/test_feed_boost_visibility.py
git commit -m "fix: stop excluding every boosted post from the feed"
```

---

## Self-Review Notes

Spec coverage:

| Spec section | Task |
|---|---|
| The classifier, three `as:Public` spellings, string/list/absent normalisation | Task 1 |
| Reads the object not the activity | Task 1 (docstring + contract), exercised by Task 2 |
| Refusal at `create_post` / `create_post_reply`, distinct reasons | Task 2 |
| DMs unaffected | Task 2 Step 6 |
| Removing the incorrect gate | Task 3 |
| Ordering dependency | Global Constraints + Task 3 preamble |
| Factory produces production shape; feed tests re-pointed | Task 3 Steps 1-3 |
| `private`'s misleading name documented | Task 1 docstring, Task 3 factory docstring |

Out-of-scope items in the spec (a `visibility` column, renaming `private`, letting public microblog posts into discovery, purging stored content, outbound visibility) correctly have no task.

Symbols are consistent throughout: `activitypub_visibility`, `_addressing_list`, `AS_PUBLIC`, `make_post(..., microblog=False)`, `BOOST_CLAUSE`.

Known risk carried into implementation: Task 2's tests call `create_post` directly with a hand-built activity. If `Post.new` requires object fields the fixtures omit, add them to `note_activity()` rather than weakening an assertion — and if `create_post` raises rather than returning `None`, report it instead of adjusting the test to expect an exception.

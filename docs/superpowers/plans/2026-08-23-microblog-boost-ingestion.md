# Microblog Boost Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PieFed ingest boosts (`Announce`) of microblog posts from Mastodon-style accounts that local users follow, record them, remove them on un-boost, and surface them in the feed.

**Architecture:** `process_microblog_announce()` in `app/activitypub/util.py` is currently an empty stub that silently drops every such activity. It gains a trust gate that runs before any network I/O, a local-post short circuit, a top-level-only guard, and delegation to the existing `create_resolved_object()` for creation. Boosts are stored in the already-present but entirely unused `PostBoost` model and `Post.post_boosts` JSON cache. A new `Undo`/`Announce` branch removes them.

**Tech Stack:** Python 3, Flask, SQLAlchemy, Celery, PostgreSQL, pytest/unittest, httpx.

**Spec:** `docs/superpowers/specs/2026-08-23-microblog-boost-ingestion-design.md`

## Global Constraints

- The trust gate MUST run before any outbound HTTP request. An unauthenticated remote party must never be able to make PieFed fetch a URL of their choosing.
- Never read the acting actor from an inner object. Only `request_json['actor']` is HTTP-signature-verified. See the comment at `app/activitypub/routes.py:819`.
- Do not reimplement the `attributedTo` / domain-match impersonation check. `create_resolved_object()` already performs it at `app/activitypub/util.py:3680-3702`.
- No database migration. `PostBoost` and `Post.post_boosts` already exist via `migrations/versions/c831b9c7eee9_post_boost.py`.
- Every exit path calls `log_incoming_ap()` with a distinct reason string. A bare `return None` is a plan violation.
- At most one outbound fetch per activity.
- Boosts of replies are out of scope. A boosted object with a truthy `inReplyTo` is logged and ignored.
- Rebase onto current `origin/main` before starting. The local branch is 111 commits ahead and 109 behind, and these functions have moved before.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `app/utils.py` | `boost_cache_entries()` — pure row-to-JSON shaping for the boost cache | Modify |
| `app/activitypub/util.py` | `announce_target_uri()`, `is_top_level()` pure helpers; `announcer_is_followed()`, `record_boost()`, `remove_boost()`; the real `process_microblog_announce()` | Modify |
| `app/models.py` | `Post.update_boost_cache()` | Modify |
| `app/activitypub/routes.py` | Announce dispatch reorder; new `Undo`/`Announce` branch | Modify |
| `app/utils.py` | Feed query gains an `EXISTS` over `post_boost` | Modify |
| `tests/test_microblog_announce.py` | Unit tests for the pure helpers | Create |
| `tests/test_boost_cache_entries.py` | Unit tests for cache shaping | Create |
| `FEDERATION.md` | Document boost ingestion | Modify |

Tests follow `tests/test_microblog_content_to_title.py`: `unittest.TestCase`, no Flask app context, no database. `tests/test_activitypub_util.py` is **not** a model to copy — it requires a live database, live network, and a hardcoded username.

---

## Task 1: Pure helpers for reading an Announce

**Files:**
- Modify: `app/activitypub/util.py` (add two module-level functions immediately above `def process_microblog_announce`, currently line 3418)
- Test: `tests/test_microblog_announce.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `announce_target_uri(activity: dict) -> Union[str, None]`
  - `is_top_level(post_data: dict) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_microblog_announce.py`:

```python
import unittest

from app.activitypub.util import announce_target_uri, is_top_level


class TestAnnounceTargetUri(unittest.TestCase):

    def test_string_object(self):
        """Mastodon sends the boosted object as a bare URI string"""
        self.assertEqual(
            announce_target_uri({'type': 'Announce', 'object': 'https://m.example/notes/1'}),
            'https://m.example/notes/1')

    def test_dict_object_uses_id(self):
        """Some platforms embed the object; its id is the URI"""
        self.assertEqual(
            announce_target_uri({'type': 'Announce', 'object': {'id': 'https://m.example/notes/2'}}),
            'https://m.example/notes/2')

    def test_missing_object(self):
        """An Announce with no object yields None rather than raising"""
        self.assertIsNone(announce_target_uri({'type': 'Announce'}))

    def test_empty_string_object(self):
        """An empty string is not a usable URI"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': ''}))

    def test_dict_object_without_id(self):
        """An embedded object with no id yields None"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': {'type': 'Note'}}))

    def test_dict_object_with_non_string_id(self):
        """A non-string id is rejected rather than returned"""
        self.assertIsNone(announce_target_uri({'type': 'Announce', 'object': {'id': 12345}}))

    def test_non_dict_activity(self):
        """A malformed activity yields None rather than raising"""
        self.assertIsNone(announce_target_uri('not a dict'))


class TestIsTopLevel(unittest.TestCase):

    def test_no_in_reply_to_key(self):
        """An object with no inReplyTo is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'content': 'hello'}))

    def test_in_reply_to_none(self):
        """inReplyTo explicitly null is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'inReplyTo': None}))

    def test_in_reply_to_empty_string(self):
        """inReplyTo as an empty string is top-level"""
        self.assertTrue(is_top_level({'type': 'Note', 'inReplyTo': ''}))

    def test_in_reply_to_set(self):
        """An object with a real inReplyTo is a reply, not top-level"""
        self.assertFalse(is_top_level({'type': 'Note', 'inReplyTo': 'https://m.example/notes/1'}))

    def test_non_dict(self):
        """Malformed data is not top-level"""
        self.assertFalse(is_top_level(None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_microblog_announce.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'announce_target_uri' from 'app.activitypub.util'`

- [ ] **Step 3: Write minimal implementation**

In `app/activitypub/util.py`, directly above `def process_microblog_announce`:

```python
def announce_target_uri(activity: dict) -> Union[str, None]:
    """Return the URI of the object an Announce (or Undo/Announce) refers to.

    Mastodon sends the object as a bare URI string. Some platforms embed the
    object, in which case its 'id' is the URI. Returns None if neither is usable.
    """
    if not isinstance(activity, dict):
        return None
    obj = activity.get('object')
    if isinstance(obj, str):
        return obj if obj else None
    if isinstance(obj, dict):
        obj_id = obj.get('id')
        return obj_id if isinstance(obj_id, str) and obj_id else None
    return None


def is_top_level(post_data: dict) -> bool:
    """True if a fetched object is a top-level post rather than a reply."""
    if not isinstance(post_data, dict):
        return False
    return not post_data.get('inReplyTo')
```

`Union` is already imported at `app/activitypub/util.py:10`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_microblog_announce.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add tests/test_microblog_announce.py app/activitypub/util.py
git commit -m "feat: add pure helpers for reading Announce activities"
```

---

## Task 2: Boost cache shaping

**Files:**
- Modify: `app/utils.py` (add a module-level function; place it near `microblog_content_to_title`, around line 990)
- Test: `tests/test_boost_cache_entries.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `boost_cache_entries(rows) -> list` — takes an iterable of `(user_id, ap_id, display_name, created_at)` tuples and returns the list of dicts stored in `Post.post_boosts`.

This is separated from the database query so the JSON shape, which is where format bugs live, is unit-testable without a database.

- [ ] **Step 1: Write the failing test**

Create `tests/test_boost_cache_entries.py`:

```python
import unittest
from datetime import datetime

from app.utils import boost_cache_entries


class TestBoostCacheEntries(unittest.TestCase):

    def test_single_remote_booster(self):
        """A remote user's ap_id and ISO timestamp are carried into the cache"""
        rows = [(7, 'https://m.example/users/alice', 'alice', datetime(2026, 8, 23, 12, 30, 0))]
        self.assertEqual(boost_cache_entries(rows), [{
            'user_id': 7,
            'ap_id': 'https://m.example/users/alice',
            'display_name': 'alice',
            'created_at': '2026-08-23T12:30:00',
        }])

    def test_local_booster_has_no_ap_id(self):
        """A local user has ap_id None; the cache stores an empty string"""
        rows = [(3, None, 'bob', datetime(2026, 8, 23, 9, 0, 0))]
        self.assertEqual(boost_cache_entries(rows)[0]['ap_id'], '')

    def test_missing_timestamp(self):
        """A null created_at becomes an empty string rather than raising"""
        rows = [(3, None, 'bob', None)]
        self.assertEqual(boost_cache_entries(rows)[0]['created_at'], '')

    def test_order_preserved(self):
        """Input order is preserved; the caller decides the ordering"""
        rows = [
            (1, None, 'first', datetime(2026, 8, 23, 10, 0, 0)),
            (2, None, 'second', datetime(2026, 8, 23, 11, 0, 0)),
        ]
        names = [entry['display_name'] for entry in boost_cache_entries(rows)]
        self.assertEqual(names, ['first', 'second'])

    def test_empty(self):
        """No boosts yields an empty list, not None"""
        self.assertEqual(boost_cache_entries([]), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_boost_cache_entries.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'boost_cache_entries' from 'app.utils'`

- [ ] **Step 3: Write minimal implementation**

In `app/utils.py`:

```python
def boost_cache_entries(rows) -> list:
    """Shape (user_id, ap_id, display_name, created_at) rows into the post_boosts cache.

    Kept separate from the query so the stored JSON shape can be tested without
    a database. Input order is preserved; the caller chooses the ordering.
    """
    return [
        {
            'user_id': user_id,
            'ap_id': ap_id if ap_id else '',
            'display_name': display_name,
            'created_at': created_at.isoformat() if created_at else '',
        }
        for user_id, ap_id, display_name, created_at in rows
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_boost_cache_entries.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add tests/test_boost_cache_entries.py app/utils.py
git commit -m "feat: add boost cache entry shaping"
```

---

## Task 3: Boost storage

**Files:**
- Modify: `app/models.py` — add `Post.update_boost_cache()` immediately after `Post.update_reaction_cache()`, which ends at line 2713
- Modify: `app/activitypub/util.py` — add `record_boost()` and `remove_boost()` above `announce_target_uri` from Task 1; extend the model import at line 27-30

**Interfaces:**
- Consumes: `boost_cache_entries()` from Task 2.
- Produces:
  - `Post.update_boost_cache() -> None`
  - `record_boost(post: Post, user: User) -> None`
  - `remove_boost(post: Post, user: User) -> None`

Idempotency uses query-then-insert rather than a unique constraint. A constraint would need a migration, and the duplicate suppression at `app/activitypub/routes.py:647` is a Redis key with a 90-second expiry — replay protection, not durable dedup. Redelivery after that window must not double-count.

- [ ] **Step 1: Add the cache method to Post**

In `app/models.py`, immediately after the end of `update_reaction_cache()` (line 2713):

```python
    def update_boost_cache(self):
        from app.utils import boost_cache_entries
        rows = db.session.query(PostBoost.user_id, User.ap_id, User.user_name, PostBoost.created_at). \
            join(User, User.id == PostBoost.user_id). \
            filter(PostBoost.post_id == self.id). \
            order_by(PostBoost.created_at.desc()).all()
        self.post_boosts = boost_cache_entries(rows)
```

`PostBoost` is defined later in the same module (line 4255) but resolves at call time. `update_reaction_cache` uses the same local-import-from-`app.utils` idiom.

- [ ] **Step 2: Add the import to util.py**

In `app/activitypub/util.py`, extend the `from app.models import ...` block at lines 27-30 with `UserFollower` and `PostBoost`. Neither is currently imported there.

- [ ] **Step 3: Add the write helpers to util.py**

```python
def record_boost(post: Post, user: User) -> None:
    """Record that `user` boosted `post`, idempotently, and refresh the cache.

    Idempotent by query-then-insert: the same Announce can be redelivered after
    the 90 second Redis duplicate window in routes.py has expired.
    """
    existing = db.session.query(PostBoost).filter_by(user_id=user.id, post_id=post.id).first()
    if existing:
        return
    db.session.add(PostBoost(user_id=user.id, post_id=post.id))
    db.session.commit()
    post.update_boost_cache()
    db.session.commit()


def remove_boost(post: Post, user: User) -> None:
    """Remove `user`'s boost of `post` and refresh the cache.

    A missing row is a successful no-op, not a failure — remote instances re-send.
    """
    existing = db.session.query(PostBoost).filter_by(user_id=user.id, post_id=post.id).first()
    if existing is None:
        return
    db.session.delete(existing)
    db.session.commit()
    post.update_boost_cache()
    db.session.commit()
```

- [ ] **Step 4: Verify the module still imports**

Run: `python -c "import app.activitypub.util, app.models"`
Expected: no output, exit 0. A `NameError` here means the `PostBoost` import in Step 2 was missed.

- [ ] **Step 5: Run the existing test suite for regressions**

Run: `pytest tests/test_microblog_announce.py tests/test_boost_cache_entries.py tests/test_microblog_content_to_title.py -v`
Expected: PASS, 20 tests

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/activitypub/util.py
git commit -m "feat: add boost recording and removal"
```

---

## Task 4: Implement process_microblog_announce

**Files:**
- Modify: `app/activitypub/util.py:3418-3435` — replace the stub body
- Modify: `FEDERATION.md`

**Interfaces:**
- Consumes: `announce_target_uri()`, `is_top_level()` (Task 1); `record_boost()` (Task 3); existing `find_actor_or_create_cached()` (line 322), `remote_object_to_json()` (line 3621), `create_resolved_object()` (line 3678), `find_microblogging_community()` (line 4030), `log_incoming_ap()` (line 3957).
- Produces: `process_microblog_announce(request_json, id, store_ap_json) -> Union[Post, None]` — signature unchanged, so the call site at `app/activitypub/routes.py:868` is unaffected.

- [ ] **Step 1: Add the trust gate helper**

In `app/activitypub/util.py`, above `record_boost`:

```python
def announcer_is_followed(user_id: int) -> bool:
    """True if at least one local user follows the remote user with this id.

    Called before any outbound fetch, so that an unfollowed remote party cannot
    make this instance request a URL of their choosing.
    """
    return db.session.query(UserFollower.id).filter(
        UserFollower.remote_user_id == user_id,
        UserFollower.is_inward == False).first() is not None
```

- [ ] **Step 2: Replace the stub body**

Replace the whole of `process_microblog_announce` — the docstring and the two-line body at lines 3418-3435 — with:

```python
def process_microblog_announce(request_json, id, store_ap_json) -> Union[Post, None]:
    """Ingest a boost of a microblog post from an account a local user follows.

    Top-level posts only. Boosted replies are ignored: backfilling absent
    ancestors would mean unbounded recursion against untrusted hosts.
    """
    saved_json = request_json if store_ap_json else None

    uri = announce_target_uri(request_json)
    if not uri:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Announce has no object URI')
        return None

    # Trust gate. Must stay above every network call in this function.
    announcer = find_actor_or_create_cached(request_json['actor'], create_if_not_found=False)
    if not announcer or not isinstance(announcer, User):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Announce actor is not a known user')
        return None
    if announcer.banned:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, f'{announcer.ap_id} is banned')
        return None
    if not announcer_is_followed(announcer.id):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Announce from unfollowed actor')
        return None

    # Local posts carry a full ap_id from Post.generate_ap_id(), so this resolves
    # both already-ingested remote posts and posts authored on this instance.
    post = Post.get_by_ap_id(uri)
    if post:
        record_boost(post, announcer)
        return post

    post_data = remote_object_to_json(uri)
    if not post_data:
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_FAILURE, saved_json, 'Could not fetch boosted object ' + uri)
        return None

    if not is_top_level(post_data):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Boosted object is a reply')
        return None

    # create_resolved_object performs the attributedTo / domain-match impersonation
    # check. Do not duplicate it here.
    resolved = create_resolved_object(uri, post_data, urlparse(uri).netloc,
                                      find_microblogging_community(), id, store_ap_json)
    if not isinstance(resolved, Post):
        log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json, 'Boosted object did not resolve to a post')
        return None

    record_boost(resolved, announcer)
    return resolved
```

`urlparse` is already imported at line 11. The `APLOG_*` constants arrive via `from app.constants import *` at line 26.

- [ ] **Step 3: Verify the module imports and helpers still pass**

Run: `python -c "import app.activitypub.util" && pytest tests/test_microblog_announce.py tests/test_boost_cache_entries.py -v`
Expected: PASS, 17 tests

- [ ] **Step 4: Verify against a real instance**

This path cannot be exercised by the test harness. From a shell with the project's usual environment:

```bash
flask shell <<'EOF'
from app.activitypub.util import process_microblog_announce
from app.models import Post, PostBoost
# Replace with a real boost URI from an account a local user follows,
# and that account's actor URI.
activity = {
    'id': 'https://m.example/users/alice/statuses/1/activity',
    'type': 'Announce',
    'actor': 'https://m.example/users/alice',
    'object': 'https://other.example/users/bob/statuses/9',
}
post = process_microblog_announce(activity, activity['id'], False)
print('post:', post)
print('boosts:', PostBoost.query.filter_by(post_id=post.id).count() if post else None)
print('cache:', post.post_boosts if post else None)
EOF
```

Expected: a `Post` in the microblogs community, exactly one `PostBoost` row, and a populated `post_boosts` cache.

- [ ] **Step 5: Verify idempotency**

Re-run the same block. Expected: the same post, still exactly **one** `PostBoost` row.

- [ ] **Step 6: Verify the trust gate**

Re-run with `actor` set to an account **no** local user follows. Expected: `post: None`, no fetch attempted, and an `ActivityPubLog` row whose message is `Announce from unfollowed actor`.

- [ ] **Step 7: Document it**

In `FEDERATION.md`, under `## ActivityPub`, add:

```markdown
- Boosts (`Announce`) of top-level posts from microblogging platforms are ingested when
  the boosting account is followed by a local user. Boosted replies are not ingested.
```

- [ ] **Step 8: Commit**

```bash
git add app/activitypub/util.py FEDERATION.md
git commit -m "feat: ingest boosts of microblog posts from followed accounts"
```

---

## Task 5: Record boosts of local posts

**Files:**
- Modify: `app/activitypub/routes.py:862-875`

**Interfaces:**
- Consumes: `process_microblog_announce()` (Task 4).
- Produces: no new symbols. Changes which activities reach `process_microblog_announce`.

The early return at lines 864-866 fires for any Announce whose object URL starts with this server's name, **before** the `community is None` dispatch at line 868. A followed account boosting a post authored here therefore never reaches the function. That is likely the most common boost a PieFed instance receives.

- [ ] **Step 1: Reorder the dispatch**

Replace lines 863-870 of `app/activitypub/routes.py`:

```python
                    if isinstance(request_json['object'], str):
                        if request_json['object'].startswith('https://' + current_app.config['SERVER_NAME']):
                            log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json, 'Activity about local content which is already present')
                            return
                        if community is None:
                            post = process_microblog_announce(request_json, id, store_ap_json)
                        else:
                            post = resolve_remote_post(request_json['object'], community, id, store_ap_json)
```

with:

```python
                    if isinstance(request_json['object'], str):
                        if community is None:
                            # A microblog boost. Its object may be local content, in which
                            # case process_microblog_announce records the boost without
                            # fetching or creating anything.
                            post = process_microblog_announce(request_json, id, store_ap_json)
                        elif request_json['object'].startswith('https://' + current_app.config['SERVER_NAME']):
                            log_incoming_ap(id, APLOG_DUPLICATE, APLOG_IGNORED, saved_json, 'Activity about local content which is already present')
                            return
                        else:
                            post = resolve_remote_post(request_json['object'], community, id, store_ap_json)
```

The early return stays in place for the `community is not None` path, so community-path deduplication is unchanged.

- [ ] **Step 2: Verify a boost of a local post is recorded**

```bash
flask shell <<'EOF'
from app.activitypub.util import process_microblog_announce
from app.models import Post, PostBoost
local_post = Post.query.filter(Post.ap_id.like('https://%'), Post.instance_id == 1).first()
activity = {
    'id': 'https://m.example/users/alice/statuses/2/activity',
    'type': 'Announce',
    'actor': 'https://m.example/users/alice',   # must be followed by a local user
    'object': local_post.ap_id,
}
post = process_microblog_announce(activity, activity['id'], False)
print('same post:', post is not None and post.id == local_post.id)
print('boosts:', PostBoost.query.filter_by(post_id=local_post.id).count())
EOF
```

Expected: `same post: True`, `boosts: 1`, and no outbound fetch.

- [ ] **Step 3: Regression check — community-path dedup still works**

Send an `Announce` from a **Community** actor whose object is a local post URL, and confirm the log still records `Activity about local content which is already present` and that no post is created or modified. Query the most recent `ActivityPubLog` rows:

```bash
flask shell <<'EOF'
from app.models import ActivityPubLog
for row in ActivityPubLog.query.filter_by(direction='in').order_by(ActivityPubLog.id.desc()).limit(5):
    print(row.activity_type, row.result, row.exception_message)
EOF
```

Expected: the `Announce` from the Community actor appears with result `ignored` and message `Activity about local content which is already present`.

- [ ] **Step 4: Commit**

```bash
git add app/activitypub/routes.py
git commit -m "fix: record boosts of posts authored on this instance"
```

---

## Task 6: Handle Undo of a boost

**Files:**
- Modify: `app/activitypub/routes.py` — add a branch after the `Like`/`Dislike` branch, which ends at line 1731; extend the `app.activitypub.util` import at line 22

**Interfaces:**
- Consumes: `announce_target_uri()` (Task 1), `remove_boost()` (Task 3).
- Produces: no new symbols.

There is currently no `Undo`/`Announce` branch at all. The chain starting at line 1645 handles only `Follow`, `Delete`, `Like`/`Dislike`, `Lock`, `Block`, and `ChooseAnswer`, so boost counts can only ever increase.

- [ ] **Step 1: Extend the import**

In `app/activitypub/routes.py`, add `announce_target_uri` and `remove_boost` to the `from app.activitypub.util import ...` block that already brings in `process_microblog_announce` at line 22.

- [ ] **Step 2: Add the branch**

Immediately after the `Like`/`Dislike` branch's `return` (line 1731):

```python
                    if core_activity['object']['type'] == 'Announce':  # Undoing a boost from a microblogging platform
                        target_ap_id = announce_target_uri(core_activity['object'])
                        post = Post.get_by_ap_id(target_ap_id) if target_ap_id else None
                        if post:
                            # `user` comes from the signed outer actor, resolved at line 838.
                            # Never read the actor from the inner object.
                            remove_boost(post, user)
                            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_SUCCESS, saved_json)
                        else:
                            log_incoming_ap(id, APLOG_ANNOUNCE, APLOG_IGNORED, saved_json,
                                            'Unfound object for Undo Announce ' + str(target_ap_id))
                        return
```

No trust gate applies. Removing a boost is always safe, and gating it could strand rows if the announcer is unfollowed between the boost and the un-boost. No fetch occurs, because there is nothing to create.

- [ ] **Step 3: Verify the module imports**

Run: `python -c "import app.activitypub.routes"`
Expected: no output, exit 0

- [ ] **Step 4: Verify un-boost removes the row**

Using the post boosted in Task 4 Step 4:

```bash
flask shell <<'EOF'
from app.activitypub.routes import process_inbox_request
from app.models import Post, PostBoost
post = Post.query.filter_by(ap_id='https://other.example/users/bob/statuses/9').first()
print('before:', PostBoost.query.filter_by(post_id=post.id).count())
undo = {
    'id': 'https://m.example/users/alice/statuses/1/undo',
    'type': 'Undo',
    'actor': 'https://m.example/users/alice',
    'object': {
        'id': 'https://m.example/users/alice/statuses/1/activity',
        'type': 'Announce',
        'actor': 'https://m.example/users/alice',
        'object': post.ap_id,
    },
}
process_inbox_request(undo, False)
print('after:', PostBoost.query.filter_by(post_id=post.id).count())
print('cache:', Post.query.get(post.id).post_boosts)
EOF
```

Expected: `before: 1`, `after: 0`, cache `[]`.

- [ ] **Step 5: Verify a repeated Undo is a no-op**

Re-run the same block. Expected: `after: 0`, no exception, and a log row with result `success`.

- [ ] **Step 6: Commit**

```bash
git add app/activitypub/routes.py
git commit -m "feat: handle Undo of microblog boosts"
```

---

## Task 7: Surface boosted posts in the feed

**Files:**
- Modify: `app/utils.py:3244-3248`

**Interfaces:**
- Consumes: the `post_boost` table populated by Task 3.
- Produces: no new symbols.

Without this, ingestion is invisible bookkeeping — the existing clause matches only on the post's **author**, so a boost by a followed account of a stranger's post matches nothing.

This sits in the hot feed path, so the change is gated on measurement.

- [ ] **Step 1: Capture the baseline query plan**

Before changing anything, capture the plan for a logged-in feed query on a populated database:

```bash
flask shell <<'EOF'
from app import db
from sqlalchemy import text
sql = """EXPLAIN ANALYZE SELECT p.id FROM "post" as p
INNER JOIN "community" as c on p.community_id = c.id
WHERE (c.show_all is true OR EXISTS (SELECT 1 FROM user_follower uf
      WHERE uf.local_user_id = :uid AND uf.remote_user_id = p.user_id AND is_inward is false))
AND c.banned is false ORDER BY p.posted_at DESC LIMIT 50"""
for row in db.session.execute(text(sql), {'uid': 1}):
    print(row[0])
EOF
```

Record the total execution time. Substitute a `local_user_id` that actually follows people.

- [ ] **Step 2: Add the EXISTS clause**

In `app/utils.py`, replace lines 3244-3248:

```python
    if current_user.is_authenticated and current_user.num_following and include_following:
        sources.append("""EXISTS (SELECT 1 FROM user_follower uf
                                  WHERE uf.local_user_id = :local_user_id
                                  AND uf.remote_user_id = p.user_id AND is_inward is false)""")
        params['local_user_id'] = current_user.id
```

with:

```python
    if current_user.is_authenticated and current_user.num_following and include_following:
        sources.append("""EXISTS (SELECT 1 FROM user_follower uf
                                  WHERE uf.local_user_id = :local_user_id
                                  AND uf.remote_user_id = p.user_id AND is_inward is false)""")
        sources.append("""EXISTS (SELECT 1 FROM post_boost pb
                                  INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
                                  WHERE pb.post_id = p.id
                                  AND uf2.local_user_id = :local_user_id
                                  AND uf2.is_inward is false)""")
        params['local_user_id'] = current_user.id
```

Both `post_boost.post_id` and `post_boost.user_id` are already indexed by `migrations/versions/c831b9c7eee9_post_boost.py`.

- [ ] **Step 3: Capture the new query plan**

Re-run Step 1's block with the second `EXISTS` added to the `WHERE` clause. Compare execution time against the baseline.

**Gate:** if the added clause degrades the query beyond what the team accepts on this dataset, stop and report the numbers rather than merging. Do not silently accept a regression in the feed path.

- [ ] **Step 4: Verify a boosted post appears in the feed**

Log in as a user who follows the boosting account, load the home feed with the "subscribed" filter, and confirm the post boosted in Task 4 appears even though its author is not followed.

- [ ] **Step 5: Run the full unit test suite**

Run: `pytest tests/ -v --ignore=tests/test_activitypub_util.py`
Expected: no new failures compared to the pre-change baseline. `tests/test_activitypub_util.py` is excluded because it requires live network access and a hardcoded username.

- [ ] **Step 6: Commit**

```bash
git add app/utils.py
git commit -m "feat: surface posts boosted by followed accounts in the feed"
```

---

## Self-Review Notes

Spec coverage, section by section:

| Spec section | Task |
|---|---|
| Control flow steps 1-6 | Task 4 |
| Rejected alternatives | N/A — recorded, nothing to build |
| Data model and idempotency | Tasks 2, 3 |
| Undo / un-boost | Task 6 |
| Boosts of local posts | Task 5 |
| Feed surfacing | Task 7 |
| Error handling and logging | Task 4 (every branch logs) |
| Security considerations | Task 4 Step 6, Task 6 Step 2 |
| Testing | Tasks 1, 2; manual steps in Tasks 4-7 |

Symbols are used consistently throughout: `announce_target_uri`, `is_top_level`, `boost_cache_entries`, `announcer_is_followed`, `record_boost`, `remove_boost`, `Post.update_boost_cache`.

# Interop Phase 0a: Stored and Enforced Visibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store each post's and reply's ActivityPub audience (`public | unlisted | followers | direct`), enforce it on every read surface, then accept followers-only content at ingest instead of refusing it.

**Architecture:** One new column per table, filled at ingest by the existing classifier `activitypub_visibility`. One predicate module, `app/visibility.py`, answers "may this viewer see this object" (Python, ORM and raw-SQL forms). Every read surface calls it. Hidden replies render as placeholders that keep their public children. The ingest refusal of `followers` is lifted only in the last task, after all enforcement has landed. `direct` stays refused; converting direct messages to chat is a separate plan.

**Tech Stack:** Flask, SQLAlchemy, Alembic (Flask-Migrate), Jinja2, pytest via `./run_tests.sh` (podman-compose Postgres and Redis).

**Spec:** `docs/superpowers/specs/2026-10-01-activitypub-interop-design.md` (D6, D7, D17), plus session rulings D18 and D19 below.

## Global Constraints

- Values are exactly `'public'`, `'unlisted'`, `'followers'`, `'direct'`, held in constants in `app/constants.py`.
- The column is `visibility`, `String(10)`, `nullable=False`, `server_default='public'`, indexed, on both `post` and `post_reply`.
- This is the fork's first migration. It carries `branch_labels = ('fork',)` (D17).
- Listings (All, Popular, Subscribed community source, topic, feed, domain, instance, tag, community page, search, sitemap, every RSS feed) show `public` only.
- The followed-author feed source and profile pages show `public`, `unlisted`, and `followers` only when the viewer follows the author (accepted, outward follow) or is the author.
- ActivityPub object and collection routes serve `public` and `unlisted` only. A followers-only object returns 404 to every requester (D7). A signed fetch from a follower's server gets no exception in this plan.
- **D18:** a reply the viewer may not see renders as a placeholder ("Visible to followers only") with no author, body, score or actions. Its visible children stay in the tree. The API returns a stub: `visibility: "followers"`, null content and null author.
- **D19:** admins, staff and community moderators get no exemption in views, trees or listings. They see followers-only content only through the report queue. The report rows already snapshot the body, for example `report.targets.orig_comment_body` in `app/templates/admin/reports/comment_report.html`.
- Archived posts: the archive JSON written by `archive_post` (`app/utils.py`) never contains the body or author of a non-public reply. It stores a stub instead.
- Every app-side import is at module top (project rule: no inline imports in `app/`), except where an existing function already imports inline to break a known cycle. In that case, extend its existing import line.
- Line numbers below are dated 2026-10-01. Find each site by its function name and read it before editing.
- One commit per task. TDD: write the failing test first.

## Review Focus

1. **A pending follow must not unlock followers-only content.** `UserFollower.is_accepted` is `None` while a request is pending. The predicate requires `is_accepted IS TRUE` and `is_inward IS FALSE`. Pinned in Task 3.
2. **An Update must not change visibility.** Mastodon cannot change a status's visibility on edit, but a hostile peer could send an `Update` that widens a followers-only reply to public. `update_post_from_activity` and `update_post_reply_from_activity` must leave `visibility` untouched. Pinned in Task 2.
3. **Boosts.** A boost of a followers-only post reaches the feed through `FOLLOWED_BOOSTER_SQL`. The boosted post must still pass the viewer predicate: following the booster does not grant the author's audience. Pinned in Task 7.
4. **Unknown or legacy values.** A row with `visibility` NULL (impossible after the migration, but possible in hand-built test rows) or an unexpected string must be treated as not visible, except NULL, which counts as `public` to match the server default. Pinned in Task 3.
5. **Children of a hidden reply in `continue_discussion`.** The permalink to a public reply whose *parent* is followers-only must still render the public reply. Only the parent becomes a placeholder. Pinned in Task 5.

---

## File Structure

- Create `app/visibility.py`: the predicate (`can_view`, `visible_to_clause`, `listable_clause`, `listable_sql`, `visible_to_sql`, `RestrictedReply`, `mark_restricted`).
- Create `migrations/versions/a7e3f1c2b9d4_visibility.py`: the column, index and fork branch label.
- Create `app/templates/post/_post_reply_restricted.html`: the placeholder.
- Modify `app/constants.py`: the four constants.
- Modify `app/models.py`: the columns, `Post.new`, `PostReply.new`.
- Modify `app/activitypub/util.py`: the ingest refusals (Task 11) and notifications (Task 9).
- Modify the read surfaces listed per task.
- Tests go in `tests/test_visibility_*.py`, with a shared builder in `tests/factories.py`.

---

### Task 1: Constants, column, migration

**Files:**
- Modify: `app/constants.py` (append near `POST_STATUS_*`, around line 19)
- Modify: `app/models.py` (`Post` columns around :2488, `PostReply` columns around :3889)
- Create: `migrations/versions/a7e3f1c2b9d4_visibility.py`
- Test: `tests/test_visibility_column.py`

**Interfaces:**
- Produces: `VISIBILITY_PUBLIC`, `VISIBILITY_UNLISTED`, `VISIBILITY_FOLLOWERS`, `VISIBILITY_DIRECT`, `VISIBILITIES` in `app.constants`; `Post.visibility`, `PostReply.visibility` (str).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_visibility_column.py
from app import db
from app.constants import VISIBILITIES, VISIBILITY_PUBLIC
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user


def test_constants_are_the_four_activitypub_audiences():
    assert VISIBILITIES == ('public', 'unlisted', 'followers', 'direct')


def test_new_rows_default_to_public(db_session):
    author = make_user(make_instance('m.example'), 'alice')
    post = make_post(make_community(), author, 'https://m.example/p/1')
    reply = make_post_reply(post, author)
    assert post.visibility == VISIBILITY_PUBLIC
    assert reply.visibility == VISIBILITY_PUBLIC


def test_server_default_fills_rows_inserted_without_the_column(db_session):
    author = make_user(make_instance('m.example'), 'alice')
    post = make_post(make_community(), author, 'https://m.example/p/1')
    db.session.execute(db.text("UPDATE post SET visibility = DEFAULT WHERE id = :id"), {'id': post.id})
    db.session.expire_all()
    assert db.session.get(type(post), post.id).visibility == 'public'
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `./run_tests.sh tests/test_visibility_column.py -v`
Expected: FAIL with `ImportError: cannot import name 'VISIBILITIES'`.

- [ ] **Step 3: Implement**

`app/constants.py`:

```python
# ActivityPub audience of a Post or PostReply, classified at ingest by
# app.activitypub.util.activitypub_visibility and enforced by app/visibility.py.
VISIBILITY_PUBLIC = 'public'
VISIBILITY_UNLISTED = 'unlisted'
VISIBILITY_FOLLOWERS = 'followers'
VISIBILITY_DIRECT = 'direct'
VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_UNLISTED, VISIBILITY_FOLLOWERS, VISIBILITY_DIRECT)
```

`app/models.py`: add the following under `private = ...` in **both** `Post` and `PostReply`:

```python
    visibility = db.Column(db.String(10), default='public', server_default='public', nullable=False, index=True)
```

`migrations/versions/a7e3f1c2b9d4_visibility.py`:

```python
"""Post and PostReply store their ActivityPub audience

Revision ID: a7e3f1c2b9d4
Revises: 8c1d4e7f2a90
Create Date: 2026-10-01 22:00:00.000000

Interop spec D6. First fork-only migration: it opens the 'fork' branch label
(spec D17) so upstream syncs merge heads instead of fighting over the chain.
Every existing row is public or unlisted (ingest refused the rest), and the two
cannot be told apart after the fact, so the backfill is 'public'.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7e3f1c2b9d4'
down_revision = '8c1d4e7f2a90'
branch_labels = ('fork',)
depends_on = None


def upgrade():
    for table in ('post', 'post_reply'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('visibility', sa.String(length=10), nullable=False,
                                          server_default='public'))
            batch_op.create_index(batch_op.f(f'ix_{table}_visibility'), ['visibility'], unique=False)


def downgrade():
    for table in ('post', 'post_reply'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(batch_op.f(f'ix_{table}_visibility'))
            batch_op.drop_column('visibility')
```

Before committing, confirm that `8c1d4e7f2a90` is still the only head: `grep -l "down_revision = '8c1d4e7f2a90'" migrations/versions/*.py` must print only the new file.

- [ ] **Step 4: Run the test and confirm it passes**

Run: `./run_tests.sh tests/test_visibility_column.py -v`
Expected: PASS. `run_tests.sh` applies migrations, so this also exercises the migration.

- [ ] **Step 5: Commit**

```bash
git add app/constants.py app/models.py migrations/versions/a7e3f1c2b9d4_visibility.py tests/test_visibility_column.py
git commit -m "feat: Post and PostReply store their ActivityPub visibility (interop D6)"
```

---

### Task 2: Ingest records visibility; Update never changes it

**Files:**
- Modify: `app/models.py`, `Post.new` (:2619): extend its existing inline `from app.activitypub.util import ...` line with `activitypub_visibility`, and pass `visibility=` into the `Post(...)` constructor (near `private=private`, :2656).
- Modify: `app/models.py`, `PostReply.new` (:3949): set `visibility` from `request_json['object']` when `request_json` is given, otherwise `'public'`. The same inline-import approach applies; `PostReply.new` already imports inline from `app.utils`, so add a separate inline import of `activitypub_visibility` from `app.activitypub.util` with the same `# cycle:` comment style.
- Test: `tests/test_visibility_ingest_stored.py`

**Interfaces:**
- Consumes: Task 1 columns and constants.
- Produces: federated rows carry the classifier's answer. Local rows carry `'public'` through the column default, so `make_post` and `make_reply` in `app/shared/` need no change.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_visibility_ingest_stored.py
import pytest

from tests.factories import make_community, make_instance, make_site, make_user
from tests.test_visibility_ingest import FOLLOWERS, PUBLIC, note_activity


@pytest.fixture
def author(db_session):
    return make_user(make_instance('m.example'), 'alice')


def test_public_post_stores_public(db_session, author):
    from app.activitypub.util import create_post
    make_site()
    post = create_post(False, make_community(), note_activity([PUBLIC], [FOLLOWERS]), author)
    assert post.visibility == 'public'


def test_unlisted_post_stores_unlisted(db_session, author):
    from app.activitypub.util import create_post
    make_site()
    post = create_post(False, make_community(), note_activity([FOLLOWERS], [PUBLIC]), author)
    assert post.visibility == 'unlisted'


def test_update_cannot_widen_visibility(db_session, author):
    """Review focus 2: a hostile Update re-addressed to Public leaves visibility alone."""
    from app import db
    from app.activitypub.util import create_post, update_post_from_activity
    make_site()
    post = create_post(False, make_community(), note_activity([FOLLOWERS], [PUBLIC]), author)
    assert post.visibility == 'unlisted'
    update = note_activity([PUBLIC], [])
    update['type'] = 'Update'
    update['object']['content'] = '<p>edited</p>'
    update_post_from_activity(post, update)
    db.session.refresh(post)
    assert post.visibility == 'unlisted'
```

Add a matching unlisted-reply test using `create_post_reply`. Copy the fixture shape from `tests/test_visibility_ingest.py::test_followers_only_reply_is_refused`, but use unlisted addressing and assert `reply.visibility == 'unlisted'`.

- [ ] **Step 2: Run the tests and confirm the first two fail**

Run: `./run_tests.sh tests/test_visibility_ingest_stored.py -v`
Expected: `test_unlisted_post_stores_unlisted` FAILS (`'public' != 'unlisted'`). `test_update_cannot_widen_visibility` passes already. Keep it as a regression pin and confirm by reading `update_post_from_activity` that it never assigns `visibility`.

- [ ] **Step 3: Implement**

In `Post.new`, beside the existing `private` logic:

```python
        visibility = activitypub_visibility(request_json['object'])
```

Then pass `visibility=visibility` to `Post(...)`.

In `PostReply.new`, before constructing `reply`:

```python
        visibility = activitypub_visibility(request_json['object']) \
            if request_json and isinstance(request_json.get('object'), dict) else 'public'
```

Then pass `visibility=visibility` to `PostReply(...)`.

- [ ] **Step 4: Run the tests**

Run: `./run_tests.sh tests/test_visibility_ingest_stored.py tests/test_visibility_ingest.py tests/test_post_private_is_only_the_microblog_marker.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_visibility_ingest_stored.py
git commit -m "feat: federated posts and replies record their classified visibility"
```

---

### Task 3: The predicate module

**Files:**
- Create: `app/visibility.py`
- Modify: `tests/factories.py` (add `make_visibility_world`)
- Test: `tests/test_visibility_predicate.py`

**Interfaces:**
- Produces:
  - `can_view(obj, viewer_id: int | None) -> bool`: `obj` is a `Post` or `PostReply`.
  - `visible_to_clause(model, viewer_id: int | None)`: a SQLAlchemy boolean expression for `Post` or `PostReply`.
  - `listable_clause(model)`: an expression for `model.visibility == 'public'`.
  - `listable_sql(alias: str) -> str`: `"<alias>.visibility = 'public'"`.
  - `visible_to_sql(alias: str) -> str`: a raw-SQL predicate that binds `:visibility_viewer_id`.
  - `RestrictedReply`: a frozen dataclass `(id: int, depth: int, parent_id: int | None, post_id: int)`.
  - `mark_restricted(tree: list[dict], viewer_id) -> list[dict]`: replaces each entry's `'comment'` with a `RestrictedReply` when `can_view` is False, and sets `entry['restricted'] = True`.
  - `make_visibility_world()` in tests/factories, returning a `SimpleNamespace` with `author` (remote), `follower` (local, accepted follow), `pending` (local, follow with `is_accepted=None`), `stranger` (local), `community`, `post` (followers-only microblog), `reply` (followers-only reply under a public post), `public_post`, and `public_child` (a public reply to `reply`).

- [ ] **Step 1: Write the factory and failing tests**

`tests/factories.py`:

```python
def make_visibility_world():
    """The audience fixture for the visibility plan. Instance 1 is local (fact 21)."""
    local = make_instance('piefed.test')
    remote = make_instance('m.example')
    make_site()
    author = make_user(remote, 'alice')
    follower = make_user(local, 'fran', local=True)
    pending = make_user(local, 'pat', local=True)
    stranger = make_user(local, 'sam', local=True)
    make_follow(follower, author)
    make_follow(pending, author, is_accepted=None)
    community = make_community()
    post = make_post(community, author, 'https://m.example/s/1', title='', microblog=True)
    post.visibility = 'followers'
    public_post = make_post(community, author, 'https://m.example/s/2')
    reply = make_post_reply(public_post, author, 'secret reply')
    reply.visibility = 'followers'
    db.session.commit()
    public_child = make_post_reply(public_post, stranger, 'public child')
    public_child.parent_id = reply.id
    public_child.depth = 1
    db.session.commit()
    return SimpleNamespace(author=author, follower=follower, pending=pending, stranger=stranger,
                           community=community, post=post, reply=reply,
                           public_post=public_post, public_child=public_child)
```

`make_follow`, `make_site`, `SimpleNamespace` and `db` are already imported or defined in `tests/factories.py`. If `make_site` there takes arguments, call it the way `tests/test_visibility_ingest.py` does.

```python
# tests/test_visibility_predicate.py
from app import db
from app.models import Post, PostReply
from app.visibility import (can_view, listable_sql, mark_restricted, visible_to_clause,
                            RestrictedReply)
from tests.factories import make_visibility_world


def test_open_audiences_are_visible_to_everyone(db_session):
    w = make_visibility_world()
    for value in ('public', 'unlisted'):
        w.post.visibility = value
        assert can_view(w.post, None)
        assert can_view(w.post, w.stranger.id)


def test_followers_only_rules(db_session):
    w = make_visibility_world()
    assert can_view(w.post, w.follower.id)
    assert can_view(w.post, w.author.id)
    assert not can_view(w.post, w.stranger.id)
    assert not can_view(w.post, None)


def test_pending_follow_does_not_unlock(db_session):
    """Review focus 1."""
    w = make_visibility_world()
    assert not can_view(w.post, w.pending.id)


def test_direct_and_unknown_values_are_never_visible(db_session):
    """Review focus 4."""
    w = make_visibility_world()
    for value in ('direct', 'bogus'):
        w.post.visibility = value
        assert not can_view(w.post, w.author.id + 1000)


def test_null_counts_as_public(db_session):
    w = make_visibility_world()
    w.post.visibility = None
    assert can_view(w.post, None)


def test_clause_matches_python_predicate(db_session):
    w = make_visibility_world()
    for viewer in (None, w.follower.id, w.pending.id, w.stranger.id, w.author.id):
        ids = {p.id for p in Post.query.filter(visible_to_clause(Post, viewer))}
        assert (w.post.id in ids) == can_view(w.post, viewer), viewer


def test_listable_sql_shape():
    assert listable_sql('p') == "p.visibility = 'public'"


def test_mark_restricted_keeps_children(db_session):
    w = make_visibility_world()
    tree = [{'comment': w.reply, 'replies': [{'comment': w.public_child, 'replies': []}]}]
    marked = mark_restricted(tree, w.stranger.id)
    assert isinstance(marked[0]['comment'], RestrictedReply)
    assert marked[0]['restricted'] is True
    assert marked[0]['replies'][0]['comment'] is w.public_child
    assert not marked[0]['replies'][0].get('restricted')
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `./run_tests.sh tests/test_visibility_predicate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.visibility'`.

- [ ] **Step 3: Implement `app/visibility.py`**

```python
"""Who may see a Post or PostReply, by its stored ActivityPub audience (interop D6/D7).

One predicate in three shapes (Python, ORM, raw SQL) so every read surface asks
the same question. No moderator exemption (ruling D19): moderators see
followers-only content through the report queue's snapshot, nowhere else.
"""
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import and_, exists, or_

from app import db
from app.constants import VISIBILITY_FOLLOWERS, VISIBILITY_PUBLIC, VISIBILITY_UNLISTED
from app.models import UserFollower

OPEN_VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_UNLISTED)


def _follows(viewer_id: int, author_id: int) -> bool:
    return db.session.query(exists().where(
        UserFollower.local_user_id == viewer_id,
        UserFollower.remote_user_id == author_id,
        UserFollower.is_inward.is_(False),
        UserFollower.is_accepted.is_(True))).scalar()


def can_view(obj, viewer_id: Optional[int]) -> bool:
    visibility = obj.visibility if obj.visibility is not None else VISIBILITY_PUBLIC
    if visibility in OPEN_VISIBILITIES:
        return True
    if visibility != VISIBILITY_FOLLOWERS or not viewer_id:
        return False
    return viewer_id == obj.user_id or _follows(viewer_id, obj.user_id)


def visible_to_clause(model, viewer_id: Optional[int]):
    open_audience = or_(model.visibility.in_(OPEN_VISIBILITIES), model.visibility.is_(None))
    if not viewer_id:
        return open_audience
    follows = exists().where(UserFollower.local_user_id == viewer_id,
                             UserFollower.remote_user_id == model.user_id,
                             UserFollower.is_inward.is_(False),
                             UserFollower.is_accepted.is_(True))
    return or_(open_audience,
               and_(model.visibility == VISIBILITY_FOLLOWERS,
                    or_(model.user_id == viewer_id, follows)))


def listable_clause(model):
    return model.visibility == VISIBILITY_PUBLIC


def listable_sql(alias: str) -> str:
    return f"{alias}.visibility = 'public'"


def visible_to_sql(alias: str) -> str:
    """Raw-SQL twin of visible_to_clause. Binds :visibility_viewer_id (NULL for anonymous)."""
    return (f"({alias}.visibility IN ('public', 'unlisted') OR ({alias}.visibility = 'followers' AND "
            f"({alias}.user_id = :visibility_viewer_id OR EXISTS (SELECT 1 FROM user_follower vf "
            f"WHERE vf.local_user_id = :visibility_viewer_id AND vf.remote_user_id = {alias}.user_id "
            f"AND vf.is_inward IS FALSE AND vf.is_accepted IS TRUE))))")


@dataclass(frozen=True)
class RestrictedReply:
    """What a template or serializer gets in place of a reply the viewer may not see (D18).
    Carries tree position only, so no template slip can leak author or body."""
    id: int
    depth: int
    parent_id: Optional[int]
    post_id: int


def mark_restricted(tree: list, viewer_id: Optional[int]) -> list:
    for entry in tree:
        comment = entry['comment']
        if not isinstance(comment, RestrictedReply) and not can_view(comment, viewer_id):
            entry['comment'] = RestrictedReply(comment.id, comment.depth or 0, comment.parent_id, comment.post_id)
            entry['restricted'] = True
        mark_restricted(entry['replies'], viewer_id)
    return tree
```

- [ ] **Step 4: Run the tests**

Run: `./run_tests.sh tests/test_visibility_predicate.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/visibility.py tests/factories.py tests/test_visibility_predicate.py
git commit -m "feat: one visibility predicate in Python, ORM and SQL shapes"
```

---

### Task 4: Single-object gates

**Files and gates.** Each gate is placed after the existing community/unpublished checks, so the existing "visibility before deletion" order (D189) holds:
- `app/post/routes.py`: add `refuse_invisible(obj)` beside `refuse_private_community` (:88). Call it in `show_post` (after `refuse_unpublished_post`, around :157), `continue_discussion` (on `post` only, around :804; the comment itself is handled by Task 5's placeholder), `post_embed`, `post_embed_code`, `post_oembed`, `post_options`, `post_reply_options` (both post and reply), `post_source`, `post_reply_source` (reply), `post_cross_posts` and the post ical route.
- `app/activitypub/routes.py`: in `post_ap_refusal` (:2435) add `if post.visibility not in OPEN_VISIBILITIES: abort(404)` before the deleted check. In `comment_ap` (:2383), do the same for `reply` after the community check.
- `app/api/alpha/views.py`: `post_view` (CP check :313) and `reply_view` (:715). When `not can_view(obj, user_id)`, raise the same exception type the CP check raises for a missing object, so the API answers as if the object does not exist.
- Test: `tests/test_visibility_single_object.py`

**Interfaces:**
- Consumes: `can_view`, `OPEN_VISIBILITIES` from `app.visibility`.
- Produces: `refuse_invisible(obj) -> None` in `app/post/routes.py`. It calls `abort(404)` when `not can_view(obj, current_user.get_id())`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_visibility_single_object.py
import pytest

from tests.factories import make_visibility_world


def client_as(app, user):
    client = app.test_client()
    if user is not None:
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
    return client


@pytest.mark.parametrize('who, status', [('stranger', 404), ('follower', 200), (None, 404)])
def test_post_page(app, db_session, who, status):
    w = make_visibility_world()
    client = client_as(app, getattr(w, who) if who else None)
    assert client.get(f'/post/{w.post.id}').status_code == status


def test_activitypub_post_is_404_even_for_follower_servers(app, db_session):
    w = make_visibility_world()
    w.post.ap_id = None  # treat as local so post_ap serves rather than redirects
    response = app.test_client().get(f'/post/{w.post.id}', headers={'Accept': 'application/activity+json'})
    assert response.status_code == 404


def test_activitypub_comment_is_404(app, db_session):
    w = make_visibility_world()
    w.reply.ap_id = None
    response = app.test_client().get(f'/comment/{w.reply.id}', headers={'Accept': 'application/activity+json'})
    assert response.status_code == 404
```

Add one API test per view (`/api/alpha/post?id=` and `/api/alpha/comment?id=`). Use the `bearer(user)` helper from `tests/factories.py` for the follower and the stranger. Assert that the stranger's response matches the "not found" response for a nonexistent id, byte for byte, apart from the id.

If `show_post` needs Site or Language setup to render, copy the setup used by an existing `show_post` test. Find one with `grep -ln "get(f'/post/" tests/`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `./run_tests.sh tests/test_visibility_single_object.py -v`
Expected: the stranger/anonymous cases FAIL with 200 instead of 404. The ActivityPub cases FAIL with 200.

- [ ] **Step 3: Implement the gates as listed under Files.** `refuse_invisible`:

```python
def refuse_invisible(obj):
    """Interop D7: followers-only content is 404 to anyone but the author's accepted local followers.
    404, not 403, so the response does not confirm that the object exists."""
    if not can_view(obj, current_user.get_id()):
        abort(404)
```

- [ ] **Step 4: Run the new tests, then the existing post-route and ActivityPub-route suites**

Run: `./run_tests.sh tests/test_visibility_single_object.py -v && ./run_tests.sh -k "post_ap or comment_ap or show_post or post_view or reply_view" -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/post/routes.py app/activitypub/routes.py app/api/alpha/views.py tests/test_visibility_single_object.py
git commit -m "feat: followers-only posts and replies are 404 to non-followers on every single-object view"
```

---

### Task 4b: Interactive routes refuse hidden objects before any side effect

Added during execution (controller ruling after the Task 4 review). D7 makes a followers-only object invisible to non-followers. Acting on an object you cannot see (voting, replying, reporting, bookmarking, subscribing, emoji-reacting, boosting) must fail the same way viewing it does, and it must fail **before** the side effect.

**Sites:**
- Web: every route in `app/post/routes.py` that takes a `post_id` or `comment_id` and changes state (vote, reply/add_reply_inline, report, bookmark, subscribe and notification toggles, emoji reaction, boost and quote, edit/delete by a non-author). Call `refuse_invisible(obj)` at the top, after loading the object.
- API: `app/api/alpha/utils/post.py` and `app/api/alpha/utils/reply.py`. Every action that calls `shared_post.*` or `shared_reply.*` and then `post_view`/`reply_view` (for example post.py around :1578, :1588, :1597; reply.py around :501, :510, :519; and the moderator actions around post.py :1661, :1715, :1728, :1856-1911 and reply.py :711, :816, :827) checks `can_view(obj, user_id)` **first** and raises the same not-found exception (`post not found` / `comment not found`) without performing the action. D19 means moderators get no exemption, so the moderator actions are gated too.
- `show_post`: move the `refuse_invisible(post)` call above the anonymous NSFW/NSFL login redirect, so an anonymous request for a hidden NSFW post gets 404 rather than a redirect that confirms the post exists.

**Tests** (`tests/test_visibility_interactions.py`):
- For each gated web and API action, a stranger's attempt returns 404 or not-found, **and** the database is unchanged: no vote row, no bookmark, no reply, no report, no subscription.
- A follower's identical attempt succeeds. This is the positive control.
- Anonymous GET `/post/<hidden nsfw id>` returns 404, not 302.
- Add follower-200 controls to Task 4's `test_other_post_views_404_for_stranger` and `test_continue_discussion_gates_the_post_only`.

Commit: `feat: actions on followers-only content are refused before they take effect`

---

### Task 5: Reply trees keep the shape and hide the content (D18)

**Files:**
- Modify: `app/post/util.py`: at the end of `post_replies` (:142) and `get_comment_branch` (:201), wrap the returned tree in `mark_restricted(tree, viewer.id if viewer else None)`. In the archived branch, call `mark_restricted` on the converted tree too (Task 6 makes archived stubs arrive already restricted).
- Create: `app/templates/post/_post_reply_restricted.html`
- Modify: every loop that includes `post/_post_reply_teaser.html` for a tree entry: `app/templates/post/_post_replies.html` (:31, :83), `app/templates/post/_post_reply_teaser.html` (:193), `app/templates/post/continue_discussion_ajax.html` (:4), and the `themes/dillo` copies (find them with `grep -rn "post_reply=reply\['comment'\]" app/templates/themes`).
- Modify: `app/activitypub/routes.py`: the `post_replies_ap` (:2525) and `post_ap_context` (:2526) queries, plus `post_replies_for_ap` in `app/activitypub/util.py` (:252). Add `.filter(PostReply.visibility.in_(OPEN_VISIBILITIES))`. Collections omit hidden replies, because a collection has no placeholder concept.
- Modify: `app/api/alpha/utils/post.py`, `get_post_replies` (:1352), and `app/api/alpha/utils/reply.py`, the tree builder. Where an entry is a `RestrictedReply`, emit the stub from Step 3.
- Test: `tests/test_visibility_reply_tree.py`

**Interfaces:**
- Consumes: `mark_restricted`, `RestrictedReply`.
- Produces: tree entries may carry `restricted: True`, with `comment` being a `RestrictedReply`. The API stub shape is `{'id': id, 'post_id': post_id, 'path': ..., 'visibility': 'followers', 'body': None, 'creator': None}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_visibility_reply_tree.py
from app.post.util import post_replies, get_comment_branch
from app.visibility import RestrictedReply
from tests.factories import make_visibility_world
from tests.test_visibility_single_object import client_as


def test_stranger_sees_placeholder_with_public_child(app, db_session):
    w = make_visibility_world()
    tree = post_replies(w.public_post, 'new', w.stranger)
    entry = next(e for e in tree if e['comment'].id == w.reply.id)
    assert isinstance(entry['comment'], RestrictedReply)
    assert entry['replies'][0]['comment'].id == w.public_child.id


def test_follower_sees_the_reply(app, db_session):
    w = make_visibility_world()
    tree = post_replies(w.public_post, 'new', w.follower)
    entry = next(e for e in tree if e['comment'].id == w.reply.id)
    assert entry['comment'].body == 'secret reply'


def test_post_page_html_never_contains_the_body(app, db_session):
    w = make_visibility_world()
    html = client_as(app, w.stranger).get(f'/post/{w.public_post.id}').get_data(as_text=True)
    assert 'secret reply' not in html
    assert 'Visible to followers only' in html
    assert 'public child' in html


def test_permalink_to_public_child_of_hidden_parent(app, db_session):
    """Review focus 5."""
    w = make_visibility_world()
    response = client_as(app, w.stranger).get(f'/post/{w.public_post.id}/comment/{w.public_child.id}')
    assert response.status_code == 200
    assert 'public child' in response.get_data(as_text=True)
    assert 'secret reply' not in response.get_data(as_text=True)
```

Add two more tests:
- An ActivityPub replies-collection test: `GET /post/<id>/replies` with the ActivityPub Accept header must not list `w.reply.ap_id` or its local URL.
- An API `get_post_replies` test: the hidden entry has `body is None` and `visibility == 'followers'`.

Check the permalink route's exact URL in `continue_discussion`'s `@bp.route` before writing that test.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `./run_tests.sh tests/test_visibility_reply_tree.py -v`
Expected: FAIL. The tree returns the real `PostReply`, and the HTML contains `secret reply`.

- [ ] **Step 3: Implement**

`app/templates/post/_post_reply_restricted.html`:

```jinja
{# D18: a reply the viewer may not see. post_reply is an app.visibility.RestrictedReply:
   id, depth, parent_id and post_id only. Its children render normally below. #}
<div class="container comment restricted" id="comment_{{ post_reply.id }}" aria-level="{{ post_reply.depth+1 }}" role="treeitem">
    <div class="row comment_body">
        <div class="col-12 pr-0"><p class="text-muted">{{ _('Visible to followers only') }}</p></div>
    </div>
    {% if children -%}
        <div class="replies depth_{{ post_reply.depth }}" role="group">
            {% for reply in children -%}
                {% with post_reply=reply['comment'], children=reply['replies'] %}
                    {% if reply.restricted %}{% include 'post/_post_reply_restricted.html' %}{% else %}{% include 'post/_post_reply_teaser.html' %}{% endif %}
                {% endwith %}
            {% endfor -%}
        </div>
    {% endif -%}
</div>
```

In every loop listed under Files, change:

```jinja
{% include 'post/_post_reply_teaser.html' %}
```

to:

```jinja
{% if reply.restricted %}{% include 'post/_post_reply_restricted.html' %}{% else %}{% include 'post/_post_reply_teaser.html' %}{% endif %}
```

Keep each loop's existing `{% with %}` arguments.

API stub (in the reply tree serializer, where each entry is converted):

```python
if entry.get('restricted'):
    stub = entry['comment']
    return {'id': stub.id, 'post_id': stub.post_id, 'visibility': 'followers',
            'body': None, 'creator': None, 'replies': [convert(child) for child in entry['replies']]}
```

Name `convert` after the serializer's actual recursive function. Read `get_post_replies` and its helper `filter_max_depth` (:1414) first, and put the stub branch where each entry is turned into a dict.

- [ ] **Step 4: Run the tests and the existing reply-tree suites**

Run: `./run_tests.sh tests/test_visibility_reply_tree.py tests/test_post_util.py tests/test_api_post_replies.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/post/util.py app/templates/post app/templates/themes app/activitypub app/api/alpha/utils tests/test_visibility_reply_tree.py
git commit -m "feat: hidden replies render as placeholders that keep their public children (D18)"
```

---

### Task 6: Archives never store non-public reply content

**Files:**
- Modify: `app/utils.py`, `archive_post`'s `serialize_tree` (:5700-5733). For a comment whose `visibility` is not `public`/`unlisted`, emit `{'id', 'parent_id', 'depth', 'post_id', 'visibility', 'replies'}` only.
- Modify: `app/post/util.py`, `convert_archived_replies_to_tree` (:48). When `reply_data.get('visibility') == 'followers'`, return `{'comment': RestrictedReply(...), 'restricted': True, 'replies': [...]}`.
- Test: `tests/test_visibility_archive.py`

- [ ] **Step 1: Write the failing test.** Call `serialize_tree` through `archive_post` with the storage stubbed. Copy the stubbing from the existing archive tests (`grep -ln "archive_post" tests/`). Assert that the serialized JSON for `w.reply` has no `body`, `body_html` or `author_*` keys, and that `w.public_child` is present under it. Then feed that JSON to `convert_archived_replies_to_tree` and assert that the entry is a `RestrictedReply`.
- [ ] **Step 2: Run the test.** Expected: FAIL, because `body` is present.
- [ ] **Step 3: Implement.** At the top of the per-comment serializer:

```python
if comment.visibility not in ('public', 'unlisted'):
    result.append({'id': int(comment.id), 'parent_id': int(comment.parent_id) if comment.parent_id else None,
                   'depth': int(comment.depth) if comment.depth else 0, 'post_id': int(comment.post_id),
                   'visibility': comment.visibility,
                   'replies': serialize_tree(reply_dict['replies'])})
    continue
```

Adapt the variable names to the loop as it stands: `reply_dict` and `comment` are the names at :5733. In `create_real_reply`:

```python
if reply_data.get('visibility') not in (None, 'public', 'unlisted'):
    return {'comment': RestrictedReply(reply_data['id'], reply_data.get('depth', 0),
                                       reply_data.get('parent_id'), reply_data.get('post_id', post.id)),
            'restricted': True,
            'replies': [create_real_reply(child) for child in reply_data.get('replies', [])]}
```

- [ ] **Step 4: Run the test and the archive tests.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: archived threads keep only a stub for non-public replies`.

---

### Task 7: Listings, search, RSS and sitemap show public only; the followed-author feed obeys the predicate

**Sites.** For ORM sites, add `.filter(listable_clause(Post))` (or `PostReply`). For raw-SQL sites, append `listable_sql('<alias>')` to the WHERE list.

| Surface | Site (2026-10-01) | Change |
|---|---|---|
| Community source of aggregate feeds | `app/utils.py` `get_deduped_post_ids`, community-source branch beside `MICROBLOG_GATE` (:597) | `AND listable_sql('p')` |
| Followed-author and booster sources | same function, `FOLLOWED_AUTHOR_SQL` / `FOLLOWED_BOOSTER_SQL` disjuncts (:599) | AND each disjunct with `visible_to_sql('p')`, and bind `visibility_viewer_id` wherever `local_user_id` is bound |
| Topic comments | `app/topic/routes.py` `show_topic` :113 | `listable_clause(PostReply)` |
| Domain (both branches) | `app/domain/routes.py` :54, :59, RSS :130 | `listable_clause(Post)` |
| Instance | `app/instance/routes.py` `instance_posts` :268, :273 | `listable_clause(Post)` |
| Tags | `app/tag/routes.py` :40, :138, :336, :375, :404 | `listable_clause(Post)` |
| Community page posts and comments | `app/community/routes.py` `show_community` :408, :534 | `listable_clause(Post)` / `listable_clause(PostReply)` |
| Community RSS and ical | `app/community/routes.py` :772/:801, :848 | `listable_clause(Post)` |
| Web search | `app/search/routes.py` :66, :131 | `listable_clause(Post)` / `listable_clause(PostReply)` |
| API post list | `app/api/alpha/utils/post.py` `get_post_list` (:401-415), `get_post_list2` (:1043) | `listable_clause(Post)` / `listable_sql('p')` |
| API reply list (non-profile modes) | `app/api/alpha/utils/reply.py` `get_reply_list` :49, :60, :69, :99, :114 | `listable_clause(PostReply)` |
| Sitemap | `app/main/routes.py` `sitemap` :692 | `listable_clause(Post)` |
| Community outbox and featured | `app/activitypub/routes.py` :2243, :2270 | `listable_clause(Post)` |

- [ ] **Step 1: Write the failing tests** in `tests/test_visibility_listings.py`. Use `make_visibility_world()` plus one `unlisted` post, and set `w.post` to `followers`. For each surface, assert that neither the unlisted nor the followers-only post id appears in the follower's or the stranger's results, and that `w.public_post` does appear.
  - For `get_deduped_post_ids`, use `feed_ids(app, viewer, [w.community.id])` from `tests/factories.py`.
  - Feed-specific assertions:
    - The follower **does** see `w.post` (followers-only) and the unlisted post when the followed-author source is on. Check `feed_ids`' kwargs and the `include_following` parameter.
    - The stranger sees neither.
    - Review focus 3: the stranger follows a third user who boosted `w.post` (`make_boost` or the `post_boost` row shape used in `tests/test_subscribed_feed_microblogs.py`), and still does not see it.
  - For web routes, use `client_as` from Task 4 and assert on the response HTML containing the post's `/post/<id>` link.
- [ ] **Step 2: Run the tests.** Expected: FAIL across the table.
- [ ] **Step 3: Implement row by row.** Import `listable_clause`, `listable_sql` and `visible_to_sql` at module top in each touched module.
- [ ] **Step 4: Run** `./run_tests.sh tests/test_visibility_listings.py -v`, then the existing feed suites: `./run_tests.sh -k "feed or topic or domain or tag or search or sitemap or rss" -q`. Expected: all PASS.
- [ ] **Step 5: Commit** with the message `feat: listings, search, RSS and sitemap show public content only (interop D7)`.

---

### Task 8: Profiles, bookmarks and user API obey the viewer predicate

**Sites.** `visible_to_clause(model, current_user.get_id())` (ORM), or `visible_to_sql(alias)` with a bound `visibility_viewer_id` (raw SQL):
- `app/user/utils.py`: `_get_user_posts` :198, `_get_user_post_replies` :233, `_get_user_posts_and_replies` :266 (raw SQL :294-295).
- `app/user/routes.py`: `user_bookmarks` :1925, `user_bookmarks_comments` :1949, `user_alerts` :1975, `user_hidden_posts` :2077, `user_read_posts` :2142.
- `app/user/routes.py` `show_profile_rss` :2463: use `listable_clause` plus unlisted, i.e. `Post.visibility.in_(OPEN_VISIBILITIES)`, because RSS has no viewer.
- `app/api/alpha/utils/user.py`: `get_user` :60-61 (passes through the list functions; add a `viewer_id` argument if they lack one), `get_user_replies` :225.
- `app/api/alpha/utils/reply.py` `get_reply_list` by-person mode :82.

- [ ] **Step 1: Write the failing tests** in `tests/test_visibility_profiles.py`:
  - The author's profile, viewed by the stranger, does not show `w.post` or `w.reply`.
  - The follower sees both.
  - Profile RSS shows neither.
  - A bookmark the stranger made while following (simulate it by inserting the bookmark row, then deleting the follow) no longer shows the post.
  - The API `get_user` for the author, called with the stranger's bearer token, excludes both.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement site by site.** Where the existing code uses `PostReply.private` (:246, :294-295), replace that test with the visibility predicate. That part of Task 10 lands here.
- [ ] **Step 4: Run** the new tests plus `./run_tests.sh -k "profile or bookmark or user_view or get_user" -q`. Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: profiles, bookmarks and the user API obey the visibility predicate`.

---

### Task 9: Notifications reach only permitted viewers

**Sites:** `app/activitypub/util.py`: `notify_about_post_task` (:3146; loops at about :3166 and :3194), and `notify_about_post_reply` (:3291; loops at about :3297 and :3335). In each loop over `notify_id`, add `can_view(obj, notify_id)` to the existing `if`.

- [ ] **Step 1: Write the failing test** in `tests/test_visibility_notifications.py`:
  - Subscribe the stranger to `w.community` for post notifications (`NOTIF_COMMUNITY`; copy the subscription row shape from an existing `notify_about_post` test, found with `grep -ln notify_about_post_task tests/`).
  - Run `notify_about_post_task(w.post.id)`.
  - Assert that the stranger has no `Notification` row, and that a follower subscribed the same way does have one.
  - Repeat for a reply under a post the stranger subscribed to (`NOTIF_POST`).
- [ ] **Step 2: Run the test.** Expected: FAIL.
- [ ] **Step 3: Implement.** Add the import at module top: `from app.visibility import can_view`. Before adding it, check the import does not cycle, since `app.visibility` imports `app.models` only.
- [ ] **Step 4: Run** the new test plus `./run_tests.sh -k notify -q`. Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat: notifications about followers-only content reach permitted viewers only`.

---

### Task 10: Retire `PostReply.private` reads and writes

`PostReply.private` meant followers-only (`app/activitypub/util.py:4444` docstring). Its only writer is the `to[0]` rule in `PostReply.new` (:3972-3975), which is dead for followers-only content (D1438). Task 8 replaced its readers in `app/user/utils.py`.

- [ ] **Step 1:** Run `grep -rn "PostReply.private\|reply.private\|\.private\b" app --include=*.py | grep -i reply` and list every remaining reader. Each must already be covered by Tasks 5-8. If one is not, add it to the owning task's table and its test before continuing.
- [ ] **Step 2: Write the failing test.** `PostReply.new` called with a followers-addressed `request_json` (bypassing `create_post_reply`'s refusal) sets `visibility == 'followers'`, and nothing reads `private` any more: assert `'private=private' not in inspect.getsource(PostReply.new)`.
- [ ] **Step 3: Implement.** Delete the `to[0]` block and the `private=private` argument from `PostReply.new`. Leave the column in place. Dropping it is a later fork migration, once a release has shipped without readers.
- [ ] **Step 4: Run** `./run_tests.sh tests/test_visibility_*.py tests/test_post_private_is_only_the_microblog_marker.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** with the message `refactor: PostReply visibility replaces the dead to[0] private rule`.

`Post.private` is **not** touched. It is the microblog marker, `MICROBLOG_GATE` and the community listings depend on it, and it is not an access control (`tests/test_post_private_is_only_the_microblog_marker.py`).

---

### Task 11: Lift the ingest refusal for followers-only content

Do this last: every surface must enforce before followers-only rows can exist.

**Files:**
- Modify: `app/activitypub/util.py`, `create_post_reply` (:2882) and `create_post` (:3103). Change `if visibility in ('followers', 'direct'):` to `if visibility == 'direct':`, and keep the log message format.
- Modify: `tests/test_visibility_ingest.py`. `test_followers_only_post_is_refused` and `test_followers_only_reply_is_refused` become `..._is_stored_as_followers` and assert `visibility == 'followers'`. The direct tests stay unchanged.
- Leave alone: the reply backfill (`app/community/util.py:338`). Unauthenticated collection fetches never return followers-only replies, and keeping it public-only is the conservative choice.

- [ ] **Step 1: Rewrite the two tests** as described, and run them. Expected: FAIL (refused).
- [ ] **Step 2: Implement** the two-line change.
- [ ] **Step 3: Run the end-to-end check.**
  1. Ingest a followers-only Note from `m.example` through `create_post`.
  2. GET `/post/<id>` as the stranger: expect 404.
  3. GET it as the follower: expect 200.
  4. Request `feed_ids` for the follower: the post is included.
  5. Request `feed_ids` for the stranger: the post is excluded.
  6. Request the ActivityPub URL: expect 404.

  Put this in `tests/test_visibility_end_to_end.py`.
- [ ] **Step 4: Run the full suite:** `./run_tests.sh -q`. Expected: no new failures against the baseline taken before Task 1. Record the baseline count in the first task's notes.
- [ ] **Step 5: Commit** with the message `feat: accept followers-only posts and replies now that every surface enforces visibility`.

---

## Side findings to verify (not part of this plan)

The read-surface inventory flagged these. Each needs a reproduction before it is registered as a defect:

- `community_outbox` and `community_featured` (`app/activitypub/routes.py` :2236, :2263) have no `Community.private` check.
- The web search (`app/search/routes.py`) has no `Community.private` filter.
- The API `get_reply_list` and `get_post_replies` have no `Community.private` filter.

## Next plans in Phase 0

The order follows the spec's D15:
1. Direct messages to chat (D7).
2. Content-kind registry, `extensions` column, iframe helper and lint check (D8, D9).
3. Workaround registry (D12).
4. Forwarded-activity refetch (D11).
5. Interop test markers and harness skeleton (D14).
6. Analysis matrix and drift job (D2, D3).

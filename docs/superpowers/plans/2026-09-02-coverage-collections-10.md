# Sub-project 10: the ActivityPub collection endpoints — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the eight ActivityPub collection endpoints — how a remote instance enumerates a community's posts, a user's followers, a feed's communities and every actor's moderators, 111 uncovered statements — to full statement coverage, and fix the three that return HTTP 500 for an unknown actor.

**Architecture:** Tests drive each endpoint's URL through `app.test_client()`. The eight are near-identical, so one harness and one seeding surface serve all of them; tests are written to make their differences visible rather than to cover each in isolation. One new test file.

**Tech Stack:** pytest, Flask test client, SQLAlchemy, podman-compose (`./run_tests.sh`).

**Spec:** `docs/superpowers/specs/2026-09-02-coverage-collections-10-design.md`

## Global Constraints

- Defects found **inside these eight functions** are fixed test-first, each in its own commit, each proved by a mutation that fails a named test. Anything outside them is **registered, not fixed**.
- Findings are numbered from **D167**; **both** of the register's "Next free number" notes are updated in the same change.
- The coverage floor in `coverage_floors.ini` rises to the measured blended figure **rounded down**. It is currently 80.
- **Locate every code target by content, not by the line numbers in this plan.** They drift; every sub-project since 5c has found them stale, and this slice's own fixes will move things.
- **EVERY test asserts `response.status_code`.** Success paths also assert `content_type` and `Cache-Control` where the endpoint sets one — `community_featured` sets none, and that absence is itself asserted.
- **No assertion may rest on a column's declared default.** `Community.banned`, `User.banned`, `Feed.public`, `Feed.banned`, `Post.sticky`, `Post.deleted` all default `False`; `Post.status` defaults `1`. Set them explicitly wherever a test depends on them.
- Every guard is mutation-tested with **each conjunct dropped separately**, each killed by a distinct named test. Record whether each kill is an **assertion-kill or a crash-kill**, and whether it is a **sole death**.
- **Three patterns this campaign has hit repeatedly — expect them:**
  1. A filter clause whose value equals what the factory always produces **cannot be killed** by any test using that factory unmodified. Every local lookup here filters `ap_id=None`, and every local-actor factory produces exactly that.
  2. A guard behind an earlier `abort` is **never reached**, so its mutation kills nothing.
  3. A mutation stripping a clause from **two call sites at once** proves the column, not the query. Mutate one site at a time.
- A kill by `respx.models.AllMockedAssertionError` is an **infrastructure kill, not behavioural**.
- **Any docstring claim about another test must be verified true**, and must remain true after the fixes. After inverting a pin, check which branch that pin used to cover.
- **`Vary` is never absent and never bare** — Flask-Compress appends `Accept-Encoding` to every response.
- **One pytest session at a time.** Implementers run only their own file; the controller runs the full suite and supplies all coverage figures.
- **Delete nothing** the task did not create. `claude_test` and `scratch_full_cov.json` in the repository root are not ours.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/test_ap_collections.py` | **new** — all eight endpoints |
| `app/activitypub/routes.py` | Task 10 fixes only |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | findings from D167 |
| `tests/README.md`, `coverage_floors.ini` | harness facts, floor raise |

**No new factories in `tests/factories.py` are expected.** `FeedItem`, `FeedMember`, `UserFollower` and `UserBlock` are all plain FK rows; this plan defines small local seeders in the test file instead, because they are used by two or three tasks each rather than campaign-wide. **Task 1 confirms this against the models** rather than assuming — sub-project 8's spec claimed no factory was needed and was wrong.

### Facts established before this plan — do not re-derive

- **`Feed.ap_outbox_url` (`app/models.py:4114`), `Feed.ap_following_url` (`:4107`) and `Feed.ap_followers_url` (`:4106`) have NO declared default.** `feed_outbox` and `feed_following` use them directly as the response `id`, and `make_local_feed` does not set them — so `id` comes back `None` unless a test sets the column. Set it explicitly and assert on it.
- **`User.ap_followers_url` (`app/models.py:1070`) has no default either**, and `user_followers` guards on `user is not None and user.ap_followers_url` — so a plain `make_user(..., local=True)` **404s**. Every positive `user_followers` test must set that column.
- **`Post.status` defaults to `1`** and `POST_STATUS_REVIEWING` is `0` (`app/constants.py:21`), so a default post passes `community_outbox`'s `status > POST_STATUS_REVIEWING` filter. Only an explicit `status=0` exercises the excluded side.
- **`UserFollower.is_accepted` has no default** (the model comments `None = request pending`), so `user_followers`' `is_accepted == True` filter is not vacuous — but a test of the accepted side must set it `True` explicitly.
- `post_to_activity`, `post_to_page`, `community_moderators` and `default_context` are all imported into `app.activitypub.routes` and patchable there.
- `community_moderators(community_id)` returns `CommunityMember` rows (owner or moderator), which the endpoint then resolves to `User` rows.
- `SERVER_URL` is `https://test.piefed.local`; `SERVER_NAME` is `test.piefed.local`.
- `seed_community_owner(domain)` creates the Instance (id 1) AND local User (id 1) that `make_community` hardcodes against.

### Helpers each task inherits

Under subagent-driven development an implementer sees only its own brief, so
this table is how a later task learns what already exists in the file. **Read
the file before writing; do not redefine any of these.**

| Helper | Defined by | Used by |
|---|---|---|
| `collection_get(app, path)` | Task 1 | 2-9 |
| `seed_local_community(name='books')` | Task 1 | 2, 3, 4, 8, 9 |
| `_follow(local_user, follower, accepted=True)` | Task 5 | 5 |
| `_seed_local_feed(name='news', public=True)` | Task 6 | 7, 8, 9 |
| `_feed_member(feed, user)` | Task 6 | 6 |
| `_feed_item(feed, community)` | Task 8 | 9 |

`seed_actors` is imported from `tests/test_actor_profiles.py` by Task 1 and used
throughout.

### Reuse, do not rebuild

`tests/test_actor_profiles.py` already defines `seed_actors(host='peer.example') -> (site, instance)`. **Import it** — the campaign already imports helpers across test files (`record_moderation` from `test_inbox_dispatch_lock_delete.py`, `dispatch` from `test_inbox_dispatch_preamble.py`). Do not copy it.

Do NOT import `profile_get`: none of these eight endpoints checks `is_activitypub_request()`, so no `Accept` header is needed and a simpler local helper reads better.

---

### Task 1: the harness, and community_outbox's resolution

**Files:** Create `tests/test_ap_collections.py`

**Interfaces — Produces:** `collection_get(...)`, `seed_local_community(...)`, used by every later task.

- [ ] **Step 1: Confirm the seeding surface**

Read the `FeedItem`, `FeedMember`, `UserFollower` and `UserBlock` models and confirm each is a plain FK row needing no factory. Say in your report which columns each requires. If any needs more than an FK pair, say so — that changes later tasks.

- [ ] **Step 2: Write the failing tests**

```python
"""tests/test_ap_collections.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import Post
from tests.factories import make_community, make_post, make_user
from tests.test_actor_profiles import seed_actors


def collection_get(app, path):
    """GET a collection endpoint through the real route.

    No Accept header is sent and none is needed: unlike the three actor-profile
    endpoints, NONE of these eight checks `is_activitypub_request()`, so all of
    them return ActivityPub JSON to any caller. That asymmetry is registered by
    the final task; this helper exists to make it visible rather than to work
    around it.
    """
    with app.test_client() as client:
        return client.get(path)


def seed_local_community(name='books'):
    """A local community the collection lookups can resolve.

    The lookups filter `name=<actor>, banned=False, ap_id=None` -- note they
    match on `name`, NOT on `ap_profile_id` the way `community_profile` does, so
    the host does not matter here the way it did in sub-project 9.
    """
    community = make_community(name=name, host='test.piefed.local')
    db.session.commit()
    return community


def test_a_local_community_outbox_is_served(app, db_session):
    """The ordinary path. `community_outbox` has NO `'@' in actor` check and no
    `is_activitypub_request()` check, so this succeeds with no header at all --
    both asymmetries against the feed collections, which abort(400) on a remote
    actor.
    """
    seed_actors()
    seed_local_community('books')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == 'https://test.piefed.local/c/books/outbox'


def test_an_unknown_community_outbox_is_404(app, db_session):
    """`else: abort(404)`. This is the shape three of the four FEED collections
    get wrong -- they 500 instead. Pinned there, correct here.
    """
    seed_actors()

    response = collection_get(app, '/c/nosuch/outbox')

    assert response.status_code == 404


def test_a_banned_community_outbox_is_404(app, db_session):
    """`banned=False` in the lookup. Set explicitly -- `Community.banned`
    defaults to False, so leaving it alone would assert nothing.

    Note `community_profile`'s LOCAL lookup has no such guard (registered as
    D158); this endpoint's does. The two disagree about the same community.
    """
    seed_actors()
    community = seed_local_community('books')
    community.banned = True
    db.session.commit()

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 404


def test_a_remote_community_outbox_is_404(app, db_session):
    """`ap_id=None`. `make_community` never sets `ap_id`, so this test sets it
    explicitly -- otherwise the clause is unkillable, a pattern this campaign
    has hit six times.
    """
    seed_actors()
    community = seed_local_community('books')
    community.ap_id = 'books@peer.example'
    db.session.commit()

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 404


def test_the_community_outbox_sets_its_cache_control(app, db_session):
    """max-age=10. Its seven siblings use 5, 15, 120 and -- for
    `community_featured` -- nothing at all, with no evident rationale. The
    values are asserted exactly so the spread is visible in the suite.
    """
    seed_actors()
    seed_local_community('books')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=10'
```

- [ ] **Step 3: Run the file**

Run: `./run_tests.sh tests/test_ap_collections.py -q`
Expected: PASS.

- [ ] **Step 4: Mutation-test the lookup**

Drop `banned=False`, then `ap_id=None`, restoring between each. Each must be killed by a distinct named test. Record failure text, assertion-vs-crash, and sole-death status. Verify `git diff app/activitypub/routes.py` is empty.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover community_outbox's resolution and headers"
```

---

### Task 2: community_outbox's post selection

**Files:** Modify `tests/test_ap_collections.py`

The sticky/remaining split, the 50-item cap, and the three post filters.

- [ ] **Step 1: Write the failing tests**

```python
def test_sticky_posts_come_before_the_rest(app, db_session, monkeypatch):
    """Two queries, concatenated sticky-first. `post_to_activity` is doubled to
    return an identifiable marker so ORDER is observable -- the real delegate
    builds a large document and is its own future slice.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    plain = make_post(community, user, 'https://test.piefed.local/post/1')
    sticky = make_post(community, user, 'https://test.piefed.local/post/2')
    sticky.sticky = True
    plain.sticky = False
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: f'AP:{post.ap_id}')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    items = response.json['orderedItems']
    assert items == ['AP:https://test.piefed.local/post/2',
                     'AP:https://test.piefed.local/post/1']


def test_a_deleted_post_is_excluded(app, db_session, monkeypatch):
    """`Post.deleted == False`, applied to BOTH queries. Set explicitly --
    `Post.deleted` defaults to False.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.deleted = True
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []
    assert response.json['totalItems'] == 0


def test_a_post_under_review_is_excluded(app, db_session, monkeypatch):
    """`Post.status > POST_STATUS_REVIEWING` (0, app/constants.py:21).
    `Post.status` defaults to 1, which PASSES the filter, so the excluded side
    needs status set to 0 explicitly.

    `community_featured` applies no such filter -- that asymmetry is pinned in
    the next task.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.status = 0
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_a_post_in_another_community_is_excluded(app, db_session, monkeypatch):
    """`Post.community_id == community.id`, applied to both queries. Without
    this test the community filter is unkillable, since every other test seeds
    exactly one community.
    """
    seed_actors()
    community = seed_local_community('books')
    other = make_community(name='films', host='test.piefed.local')
    user = make_user(None, 'author', local=True)
    make_post(other, user, 'https://test.piefed.local/post/1')
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the post filters**

Drop `Post.deleted == False` from the STICKY query only, then from the REMAINING query only — **one site at a time**, because a mutation stripping both proves the column rather than the query. Do the same for the status filter. Record which test dies for each, and say plainly if a single-site mutation cannot be killed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover community_outbox's post selection and filters"
```

---

### Task 3: community_featured, and the two defects it pins

**Files:** Modify `tests/test_ap_collections.py`

**TWO TESTS PIN DEFECTS. FIX NEITHER.**

- [ ] **Step 1: Write the failing tests**

```python
def test_the_featured_collection_lists_sticky_posts(app, db_session, monkeypatch):
    """`community_featured` selects `sticky=True, deleted=False` and renders each
    with `post_to_page` -- a DIFFERENT delegate from `community_outbox`'s
    `post_to_activity`, which is why both are doubled separately.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.sticky = True
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_page',
                        lambda post: f'PAGE:{post.ap_id}')

    response = collection_get(app, '/c/books/featured')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['orderedItems'] == ['PAGE:https://test.piefed.local/post/1']


def test_a_non_sticky_post_is_not_featured(app, db_session, monkeypatch):
    """`sticky=True`. Set explicitly on the excluded post -- `Post.sticky`
    defaults to False, so the absence would otherwise rest on that default.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.sticky = False
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_page', lambda post: 'PAGE')

    response = collection_get(app, '/c/books/featured')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_a_featured_post_under_review_is_published_anyway(app, db_session, monkeypatch):
    """PINS a defect. `community_outbox` filters
    `Post.status > POST_STATUS_REVIEWING`; `community_featured` filters only
    `deleted=False`. So a sticky post still under review is HIDDEN from the
    outbox and PUBLISHED in the featured collection -- the same post, two
    endpoints, opposite answers.

    Status is set to 0 (POST_STATUS_REVIEWING) explicitly; the column defaults
    to 1, which would pass any filter.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.sticky = True
    post.status = 0
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_page', lambda post: 'PAGE')

    response = collection_get(app, '/c/books/featured')

    assert response.status_code == 200
    assert response.json['orderedItems'] == ['PAGE']


def test_the_featured_collection_sets_no_cache_control(app, db_session, monkeypatch):
    """PINS a defect. Every one of the seven sibling collections sets a
    Cache-Control header; this one sets none, so caching falls to whatever the
    deployment's default is.

    Asserted as absence rather than as a value, which is what makes it
    discriminating: adding any Cache-Control would fail this test.
    """
    seed_actors()
    seed_local_community('books')
    monkeypatch.setattr(activitypub_routes, 'post_to_page', lambda post: 'PAGE')

    response = collection_get(app, '/c/books/featured')

    assert response.status_code == 200
    assert 'Cache-Control' not in response.headers


def test_an_unknown_community_featured_is_404(app, db_session):
    seed_actors()

    response = collection_get(app, '/c/nosuch/featured')

    assert response.status_code == 404
```

- [ ] **Step 2: Run the file**

Expected: PASS. If `Cache-Control` IS present — perhaps set by a global `after_request` — report that: the defect would then be narrower than the spec claims, and the test must assert what is real.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover community_featured and pin its two asymmetries"
```

---

### Task 4: community_moderators_route

**Files:** Modify `tests/test_ap_collections.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_moderators_collection_lists_moderator_urls(app, db_session):
    """`community_moderators(community.id)` returns CommunityMember rows, which
    the endpoint resolves to Users and renders as `public_url()`.

    `make_community_member(user, community, is_moderator=True)` creates the row;
    read its real signature before use.
    """
    site, instance = seed_actors()
    community = seed_local_community('books')
    from tests.factories import make_community_member
    mod = make_user(instance, 'mod', local=True)
    make_community_member(mod, community, is_moderator=True)
    db.session.commit()

    response = collection_get(app, '/c/books/moderators')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['totalItems'] == 1
    assert response.json['orderedItems'] == [mod.public_url()]


def test_a_community_with_no_moderators_lists_none(app, db_session):
    """The zero-iteration side. `totalItems` is `len(moderators)`, so an empty
    community yields 0 -- and the `orderedItems` list is built unconditionally
    before the loop, so it is present and empty rather than absent.
    """
    seed_actors()
    seed_local_community('books')

    response = collection_get(app, '/c/books/moderators')

    assert response.status_code == 200
    assert response.json['totalItems'] == 0
    assert response.json['orderedItems'] == []


def test_a_non_moderator_member_is_not_listed(app, db_session):
    """`community_moderators` filters on `is_owner OR is_moderator`. A plain
    member satisfies neither, so this test is what makes that filter killable.
    """
    site, instance = seed_actors()
    community = seed_local_community('books')
    from tests.factories import make_community_member
    member = make_user(instance, 'member', local=True)
    make_community_member(member, community, is_moderator=False)
    db.session.commit()

    response = collection_get(app, '/c/books/moderators')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_the_moderators_collection_sets_a_two_minute_cache(app, db_session):
    """max-age=120 -- the longest of the eight, against community_outbox's 10
    and feed_outbox's 5, for data that changes less often than either.
    """
    seed_actors()
    seed_local_community('books')

    response = collection_get(app, '/c/books/moderators')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=120'


def test_an_unknown_community_moderators_is_404(app, db_session):
    seed_actors()

    response = collection_get(app, '/c/nosuch/moderators')

    assert response.status_code == 404
```

- [ ] **Step 2: Run the file**

Expected: PASS. `make_community_member`'s real signature governs — read it. If `community_moderators` is cached (it is a `@cache`-decorated helper in some builds), confirm `CACHE_TYPE='NullCache'` makes that inert, as it does elsewhere in this suite.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover community_moderators_route"
```

---

### Task 5: user_followers

**Files:** Modify `tests/test_ap_collections.py`

The only collection with a compound guard, a block filter and an acceptance filter.

- [ ] **Step 1: Write the failing tests**

```python
def _follow(local_user, follower, accepted=True):
    """A UserFollower row: `follower` follows `local_user`.

    `is_accepted` has NO declared default (the model comments None = request
    pending), so it is passed explicitly here and the endpoint's
    `is_accepted == True` filter is not vacuous.
    """
    from app.models import UserFollower
    row = UserFollower(local_user_id=local_user.id, remote_user_id=follower.id,
                       is_accepted=accepted)
    db.session.add(row)
    db.session.commit()
    return row


def test_a_users_followers_are_listed(app, db_session):
    """`user_followers` guards on `user is not None AND user.ap_followers_url`.
    `User.ap_followers_url` has no declared default and `make_user` never sets
    it, so a plain local user 404s -- every positive test here must set it.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    bob = make_user(instance, 'bob')
    db.session.commit()
    _follow(alice, bob)

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['type'] == 'Collection'
    assert response.json['id'] == 'https://test.piefed.local/u/alice/followers'
    assert response.json['totalItems'] == 1
    assert bob.ap_public_url in response.json['items']


def test_a_user_without_a_followers_url_is_404(app, db_session):
    """The SECOND conjunct of `user is not None and user.ap_followers_url`.
    The user exists and is local, so only the missing column can cause the 404 --
    which is what makes this test the one that kills that conjunct.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = None
    db.session.commit()

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 404


def test_an_unaccepted_follow_is_not_listed(app, db_session):
    """`UserFollower.is_accepted == True`. Passed False explicitly rather than
    left None, so the test states its premise.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    bob = make_user(instance, 'bob')
    db.session.commit()
    _follow(alice, bob, accepted=False)

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['totalItems'] == 0


def test_a_blocked_follower_is_not_listed(app, db_session):
    """The outer join against UserBlock. The endpoint's comment says it excludes
    followers "blocked by user"; read the join condition before trusting the
    direction, and seed whichever way it actually tests.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    bob = make_user(instance, 'bob')
    db.session.commit()
    _follow(alice, bob)
    from app.models import UserBlock
    db.session.add(UserBlock(blocker_id=bob.id, blocked_id=alice.id))
    db.session.commit()

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['totalItems'] == 0


def test_the_followers_collection_sets_cache_and_vary(app, db_session):
    """`user_followers` is the ONLY one of the eight that sets `Vary: Accept` --
    and the only thing it varies on is nothing, since none of the eight
    negotiates on Accept. Registered as an asymmetry; asserted here so it is
    visible.

    Flask-Compress appends Accept-Encoding to every response, so the observed
    value is 'Accept, Accept-Encoding', not the bare 'Accept' the route sets.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    db.session.commit()

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=15'
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'


def test_an_unknown_user_followers_is_404(app, db_session):
    seed_actors()

    response = collection_get(app, '/u/nosuch/followers')

    assert response.status_code == 404
```

- [ ] **Step 2: Run the file**

Expected: PASS. **The block test's direction is a real unknown** — read the outer-join condition and seed to match it. If blocking in the direction the brief guesses does not exclude the follower, try the other and report which is correct; that is a fact the register wants.

- [ ] **Step 3: Mutation-test the guards**

Drop `banned=False` from the lookup; drop `user.ap_followers_url` from the compound guard; drop `is_accepted == True`. Each killed by a distinct named test, one at a time. Record each.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover user_followers' guards, block filter and headers"
```

---

### Task 6: feed_followers — the reference shape

**Files:** Modify `tests/test_ap_collections.py`

`feed_followers` is the **only** feed collection with a correct not-found path. Covering it first gives the next three tasks a working shape to compare their broken siblings against.

- [ ] **Step 1: Write the failing tests**

```python
def _seed_local_feed(name='news', public=True):
    """A local feed the collection lookups resolve, with the AP URL columns set.

    `Feed.ap_followers_url`/`ap_following_url`/`ap_outbox_url` have NO declared
    defaults and `make_local_feed` does not set them -- `feed_outbox` and
    `feed_following` use them directly as the response `id`, so it comes back
    None unless a test sets it.
    """
    from tests.factories import make_local_feed
    feed = make_local_feed(name, public=public)
    base = f'https://test.piefed.local/f/{name}'
    feed.ap_followers_url = f'{base}/followers'
    feed.ap_following_url = f'{base}/following'
    feed.ap_outbox_url = f'{base}/outbox'
    db.session.commit()
    return feed


def _feed_member(feed, user):
    from app.models import FeedMember
    row = FeedMember(feed_id=feed.id, user_id=user.id)
    db.session.add(row)
    db.session.commit()
    return row


def test_a_feed_followers_collection_counts_its_members(app, db_session):
    """`totalItems` is a real `FeedMember` count."""
    site, instance = seed_actors()
    feed = _seed_local_feed('news', public=True)
    member = make_user(instance, 'member', local=True)
    _feed_member(feed, member)

    response = collection_get(app, '/f/news/followers')

    assert response.status_code == 200
    assert response.json['type'] == 'Collection'
    assert response.json['totalItems'] == 1


def test_the_feed_followers_items_list_is_always_empty(app, db_session):
    """PINS a defect. `totalItems` is a real count but `items` is hardcoded
    `[]`, so the document says "one follower" and lists none.

    Hiding follower lists is a defensible privacy choice, but reporting a
    non-zero count beside an empty list is self-contradictory: a consumer
    cannot tell "hidden" from "none". `user_followers`, by contrast, populates
    its items.
    """
    site, instance = seed_actors()
    feed = _seed_local_feed('news', public=True)
    member = make_user(instance, 'member', local=True)
    _feed_member(feed, member)

    response = collection_get(app, '/f/news/followers')

    assert response.status_code == 200
    assert response.json['totalItems'] == 1
    assert response.json['items'] == []


def test_a_remote_feed_followers_request_is_400(app, db_session):
    """`'@' in actor` -> abort(400). All four feed collections have this check;
    none of the community or user collections does.
    """
    seed_actors()

    response = collection_get(app, '/f/news@peer.example/followers')

    assert response.status_code == 400


def test_an_unknown_feed_followers_is_404(app, db_session):
    """`feed_followers` is the ONLY feed collection with a correct
    `if feed is not None: ... else: abort(404)`. Its three siblings return 500
    for the same request -- pinned in the next three tasks.
    """
    seed_actors()

    response = collection_get(app, '/f/nosuch/followers')

    assert response.status_code == 404


def test_the_feed_followers_collection_sets_its_cache_control(app, db_session):
    seed_actors()
    _seed_local_feed('news', public=True)

    response = collection_get(app, '/f/news/followers')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=15'
```

- [ ] **Step 2: Run the file**

Expected: PASS. Note `feed_followers` does **not** check `feed.public` — if a non-public feed is served here, that is real and worth reporting.

- [ ] **Step 3: Mutation-test the not-found path**

Delete `feed_followers`' `else: abort(404)` and confirm `test_an_unknown_feed_followers_is_404` fails. This is the exact shape its three siblings are missing. Record the kill and whether it was an assertion-kill or a crash-kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover feed_followers, the one correct feed collection"
```

---

### Task 7: feed_moderators_route, and its missing else

**Files:** Modify `tests/test_ap_collections.py`

**ONE TEST PINS A CRASH. DO NOT FIX IT.**

- [ ] **Step 1: Write the failing tests**

```python
def test_a_feed_moderators_collection_lists_its_owner(app, db_session):
    """Feeds have a single owner, wrapped in a list "in case we want to expand
    that in the future" per the source comment. Rendered as `ap_profile_id`,
    where `community_moderators_route` renders `public_url()`.
    """
    site, instance = seed_actors()
    feed = _seed_local_feed('news', public=True)
    owner = db.session.query(type(feed)).get(feed.id).user_id
    db.session.commit()

    response = collection_get(app, '/f/news/moderators')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['totalItems'] == 1


def test_an_unknown_feed_moderators_returns_500(app, db_session):
    """PINS a crash, remotely reachable.

    `feed_moderators_route` opens `if feed is not None:` and has NO `else`, so
    an unknown feed falls off the end of the function, returns None, and Flask
    raises. `feed_followers` -- twenty lines away in the same file -- gets this
    right with `else: abort(404)`.

    Any instance can trigger this with GET /f/<anything>/moderators.
    """
    seed_actors()

    response = collection_get(app, '/f/nosuch/moderators')

    assert response.status_code == 500


def test_a_remote_feed_moderators_request_is_400(app, db_session):
    seed_actors()

    response = collection_get(app, '/f/news@peer.example/moderators')

    assert response.status_code == 400
```

- [ ] **Step 2: Run the file**

Expected: PASS. **If the 500 does not materialise** — if Flask's test client re-raises rather than returning 500, or an error handler intercepts — report exactly what happens and assert that instead. The defect is that the function does not return a response; the observable form is what the test must state. You may need `app.config['PROPAGATE_EXCEPTIONS']` or the test client's `raise_server_exceptions=False`; read how the suite's other 500-adjacent tests do it before inventing an approach.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover feed_moderators_route and pin its missing else"
```

---

### Task 8: feed_outbox, and its three defects

**Files:** Modify `tests/test_ap_collections.py`

**THREE TESTS PIN DEFECTS. FIX NONE.**

- [ ] **Step 1: Write the failing tests**

```python
def _feed_item(feed, community):
    from app.models import FeedItem
    row = FeedItem(feed_id=feed.id, community_id=community.id)
    db.session.add(row)
    db.session.commit()
    return row


def test_a_feed_outbox_lists_its_communities(app, db_session):
    """The ordinary path. `id` comes from `feed.ap_outbox_url`, which has no
    declared default -- `_seed_local_feed` sets it.
    """
    seed_actors()
    feed = _seed_local_feed('news', public=True)
    community = seed_local_community('books')
    _feed_item(feed, community)

    response = collection_get(app, '/f/news/outbox')

    assert response.status_code == 200
    assert response.json['id'] == 'https://test.piefed.local/f/news/outbox'
    assert response.json['type'] == 'Collection'


def test_an_unknown_feed_outbox_returns_500(app, db_session):
    """PINS a crash, remotely reachable.

    `feed_outbox` resolves the feed and then reads `feed.public` with no None
    check, so an unknown name raises AttributeError. Same class as
    `feed_moderators_route`'s missing else, different mechanism.
    """
    seed_actors()

    response = collection_get(app, '/f/nosuch/outbox')

    assert response.status_code == 500


def test_a_non_public_feed_outbox_is_403(app, db_session):
    """`if not feed.public: abort(403)`. `public=False` passed explicitly --
    it is also the column default, so relying on it would hide the premise.
    """
    seed_actors()
    _seed_local_feed('news', public=False)

    response = collection_get(app, '/f/news/outbox')

    assert response.status_code == 403


def test_the_feed_outbox_publishes_local_only_communities(app, db_session):
    """PINS a defect. `feed_outbox`'s own comment says it "will just be the
    same as the /following collection". It is not: `feed_following` skips
    communities that are `local_only` or `private`, and `feed_outbox` applies
    no such filter.

    So the endpoint documented as equivalent publishes the URL of a community
    its twin deliberately withholds. `local_only` is set explicitly; it
    defaults to False.
    """
    seed_actors()
    feed = _seed_local_feed('news', public=True)
    community = seed_local_community('books')
    community.local_only = True
    db.session.commit()
    _feed_item(feed, community)

    response = collection_get(app, '/f/news/outbox')

    assert response.status_code == 200
    assert community.ap_public_url in response.json['items']


def test_the_feed_outbox_duplicates_items_once_per_feed(app, db_session):
    """PINS a defect. The query is

        db.session.query(FeedItem).join(Feed, FeedItem.feed_id == feed.id)

    whose ON condition never mentions the joined `Feed` table -- it compares a
    column to an already-resolved constant. So every matching FeedItem is paired
    with EVERY row in `feed`, and each community appears once per feed on the
    instance, with `totalItems` inflating to match.

    Two feeds exist here and one FeedItem, so the correct answer is 1 and the
    observed answer is 2. With a single feed the bug is invisible, which is why
    this test seeds a second one that is otherwise irrelevant.
    """
    seed_actors()
    feed = _seed_local_feed('news', public=True)
    _seed_local_feed('sports', public=True)
    community = seed_local_community('books')
    _feed_item(feed, community)

    response = collection_get(app, '/f/news/outbox')

    assert response.status_code == 200
    assert response.json['totalItems'] == 2
    assert response.json['items'] == [community.ap_public_url,
                                      community.ap_public_url]
```

- [ ] **Step 2: Run the file**

Expected: PASS. **The duplication count is a prediction, not a certainty** — it depends on how many `feed` rows exist, which `seed_actors` and the factories may influence. Run it, observe the real number, and assert that; then say in your report how many feeds existed and whether the count matched `len(feeds)`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover feed_outbox and pin its crash, its leak and its duplication"
```

---

### Task 9: feed_following

**Files:** Modify `tests/test_ap_collections.py`

**ONE TEST PINS A CRASH. DO NOT FIX IT.**

- [ ] **Step 1: Write the failing tests**

```python
def test_a_feed_following_lists_its_communities(app, db_session):
    """`id` comes from `feed.ap_following_url`. Items are `public_url()`, where
    `feed_outbox` uses `ap_public_url` -- two accessors for the same idea.
    """
    seed_actors()
    feed = _seed_local_feed('news', public=True)
    community = seed_local_community('books')
    _feed_item(feed, community)

    response = collection_get(app, '/f/news/following')

    assert response.status_code == 200
    assert response.json['id'] == 'https://test.piefed.local/f/news/following'
    assert community.public_url() in response.json['items']


def test_an_unknown_feed_following_returns_500(app, db_session):
    """PINS a crash, remotely reachable -- the third of three. Same missing
    None check as `feed_outbox`.
    """
    seed_actors()

    response = collection_get(app, '/f/nosuch/following')

    assert response.status_code == 500


def test_a_non_public_feed_following_is_403(app, db_session):
    seed_actors()
    _seed_local_feed('news', public=False)

    response = collection_get(app, '/f/news/following')

    assert response.status_code == 403


def test_feed_following_skips_local_only_communities(app, db_session):
    """`if c.local_only or c.private: continue` -- the filter `feed_outbox`
    lacks. First disjunct isolated: `private` is left False.
    """
    seed_actors()
    feed = _seed_local_feed('news', public=True)
    community = seed_local_community('books')
    community.local_only = True
    community.private = False
    db.session.commit()
    _feed_item(feed, community)

    response = collection_get(app, '/f/news/following')

    assert response.status_code == 200
    assert response.json['items'] == []


def test_feed_following_skips_private_communities(app, db_session):
    """Second disjunct. `local_only` left False, so this is the only test that
    can kill `c.private`.
    """
    seed_actors()
    feed = _seed_local_feed('news', public=True)
    community = seed_local_community('books')
    community.local_only = False
    community.private = True
    db.session.commit()
    _feed_item(feed, community)

    response = collection_get(app, '/f/news/following')

    assert response.status_code == 200
    assert response.json['items'] == []
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the skip filter**

Drop `c.local_only or`, then `or c.private`, restoring between each. Each killed by a distinct named test. Record both.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ap_collections.py
git commit -m "test: cover feed_following's guards and its visibility filter"
```

---

### Task 10: fix the three crashes

**Files:** Modify `app/activitypub/routes.py`; modify `tests/test_ap_collections.py`

**THREE FIXES, THREE SEPARATE COMMITS.** All three are the same defect class — an unknown actor reaching code that assumes it was found — and all three are remotely reachable.

**Fix A — `feed_moderators_route`.** Add the missing `else: abort(404)`. Inverts `test_an_unknown_feed_moderators_returns_500`.

**Fix B — `feed_outbox`.** Add a `None` check before `feed.public` is read. Inverts `test_an_unknown_feed_outbox_returns_500`.

**Fix C — `feed_following`.** The same. Inverts `test_an_unknown_feed_following_returns_500`.

Use `feed_followers`' existing shape as the model — it is the one sibling that gets this right, in the same file. **Match it rather than inventing a fourth spelling.**

**DO NOT fix** the cartesian-product join, the `local_only` leak, or `feed_followers`' empty `items`. Those change what peers are told and are registered by Task 11. Their pins stay as they are.

For each fix, four steps:

- [ ] **Fix A — Step 1:** Rename `test_an_unknown_feed_moderators_returns_500` to `..._is_404`, assert 404, rewrite the docstring so it no longer describes a crash.
- [ ] **Fix A — Step 2:** Run it and watch it FAIL. **Quote the failure text in your report** — that output is the proof the test discriminates.
- [ ] **Fix A — Step 3:** Apply the fix; run; expect PASS.
- [ ] **Fix A — Step 4:** Mutation-test: remove the `else` again, confirm the named test fails, restore. Then commit:

```bash
git add app/activitypub/routes.py tests/test_ap_collections.py
git commit -m "fix: return 404 for an unknown feed's moderators collection"
```

- [ ] **Fix B — Steps 5-8:** the same four steps for `feed_outbox`. Note its `if not feed.public:` currently sits OUTSIDE the `if/else` that resolves the feed — decide whether to guard early or restructure, and justify the choice. Commit:

```bash
git commit -m "fix: return 404 for an unknown feed's outbox"
```

- [ ] **Fix C — Steps 9-12:** the same four steps for `feed_following`. Commit:

```bash
git commit -m "fix: return 404 for an unknown feed's following collection"
```

- [ ] **Step 13: Check for a vacated branch.** Each inversion may have removed the only test reaching some branch — a prior sub-project lost one this way with the suite green. For each inverted test, name the branch it used to cover and the test that covers it now. If none does, add one and mutation-prove it.

- [ ] **Step 14: Audit docstrings falsified by the fixes.** Every claim in the file about the three endpoints returning 500 is now false, including cross-references in `feed_followers`' tests. A prior sub-project lost two fix rounds to exactly this.

---

### Task 11: the register, the README, and the floor

**Files:** Modify the findings register, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Register the findings**

Add a `## Sub-project 10` section to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, matching sub-project 9's shape (**read it first**). Number from **D167**; update **both** "Next free number" notes.

**Number by distinct defect, not by commit.** Every row: location, defect, impact, and how established — `measured` or `reading-level`. **Verify every line number against the file before writing it**, and remember an edit shifts every citation below it in the same file.

Register at minimum:

- Task 10's three fixes, with commits.
- **The cartesian-product join** in `feed_outbox` and `feed_following`, with the duplication factor the tests actually measured. Not fixed — say why.
- **`feed_outbox` publishes `local_only` and `private` communities** that `feed_following` skips, despite a comment calling them equivalent.
- **`community_featured` ignores the post-status filter** its sibling applies, so a post under review is hidden from the outbox and published in featured.
- **`community_featured` sets no `Cache-Control`**, and the eight use 5, 10, 10, 15, 15, 120 and none, with no evident rationale.
- **`totalItems` is the page size in `community_outbox`** — `posts` is capped at 50.
- **`feed_followers` reports a real count with `items` always `[]`.**
- **The feed lookups have neither a `banned` nor a `public` guard**, while community and user lookups filter `banned=False`; two of four feed endpoints check `public` after the fact.
- **None of the eight checks `is_activitypub_request()`**, unlike all three actor-profile endpoints.
- **Only `user_followers` sets `Vary: Accept`**, and nothing here negotiates on `Accept`.
- Any mutation that could not be killed, with the test added to kill it.

- [ ] **Step 2: Record the harness facts**

Append to `tests/README.md`'s numbered list, continuing its numbering. At minimum: that `Feed.ap_outbox_url`/`ap_following_url`/`ap_followers_url` and `User.ap_followers_url` have no defaults and are used directly as response ids; how a 500 is observed through the test client in this suite; and whatever the block-direction investigation in Task 5 established.

- [ ] **Step 3: Raise the floor**

The controller supplies the measured blended figure. Set `app/activitypub/routes.py` in `coverage_floors.ini` to that figure **rounded down**. It is currently 80. **Do not run the full suite.**

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 10's findings and raise the routes.py floor"
```

# AP interop C5: count-only collections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group actors advertise `following`, Person actors advertise `liked`, and local posts and comments carry
inline `likes`/`dislikes`/`shares` counts, each also served at its own id, all count-only.

**Architecture:** Two pure builders in `app/activitypub/util.py` (`count_collection`, and per-object
`post_count_collections` / `comment_count_collections`) feed both the object JSON and the new routes, so inline and
served counts cannot differ. New routes in `app/activitypub/routes.py` reuse the existing refusal helpers;
`comment_ap`'s checks move into `comment_ap_refusal` so the comment and its collections share one gate.

**Tech Stack:** Flask, SQLAlchemy, pytest via `./run_tests.sh` (podman test stack).

**Spec:** `docs/superpowers/specs/2026-10-12-ap-interop-c5-count-collections-design.md`

## Global Constraints

- Count-only: every new collection has `totalItems` and no items (`orderedItems: []` on the count-only
  OrderedCollections; the inline `Collection` objects carry no item property at all).
- Inline shape exactly `{"id": <url>, "type": "Collection", "totalItems": <int>}`; the route serves that dict plus
  `"@context": default_context()`.
- A NULL cached count is served as 0.
- `vote_privately` → `liked` `totalItems` 0; the property is still advertised.
- Comments carry `likes` and `dislikes`, never `shares`.
- No fetch, sleep or retry on any path (D738/D775).
- TDD; 100% line and branch coverage of changed lines; one commit per task (one per defect in fix rounds).
- Never assign `app.config` in tests; use `monkeypatch.setitem` (ratchet
  `tests/test_parallel_workers.py::TestNothingLeaksConfigIntoLaterTests`).
- No function-level imports (`tests/test_no_inline_imports.py`); import order ratchet `tests/test_import_order.py`.
- Run tests only through `./run_tests.sh <paths> -q -p no:cacheprovider`.
- Commit trailer: `Co-Authored-By: <your model> <noreply@anthropic.com>`.

## Review Focus

1. A collection URL for an object its object route refuses (local-only, private, unpublished, followers-only,
   deleted, author-blocked instance) answers exactly what the object route answers — pinned by the parity tables in
   Tasks 4 and 5.
2. A collection URL for a REMOTE post or comment is 404, not a redirect and not our partial count — Tasks 4 and 5.
3. `liked` never counts a vote in a local-only/private community, on a followers-only or deleted object, or any
   downvote — Task 3.
4. `comment_ap` behaves exactly as before the refactor — Task 5 runs the existing comment tests in
   `tests/test_ap_content_objects.py` unchanged.
5. Existing tests that compare whole `post_to_page`/`comment_model_to_json` output may need the new keys — Task 1
   runs the suites that call them.

## File Structure

- Modify `app/activitypub/util.py`: `count_collection`, `post_shares_count`, `post_count_collections`,
  `comment_count_collections`, `user_liked_count`; `post_to_page`, `comment_model_to_json`, `community_actor_data`
  gain fields.
- Modify `app/activitypub/routes.py`: Person actor `liked`; routes `community_following`, `user_liked`,
  `post_count_collection_ap`, `comment_count_collection_ap`; helpers `count_collection_response`,
  `comment_ap_refusal`.
- Create `tests/test_ap_interop_count_collections.py` (all tasks append to it).

Shared test header (Task 1 creates it; later tasks add imports they need to this block):

```python
"""Interop C5 (spec 2026-10-12): count-only `following` on a Group, `liked` on a Person, and inline `likes`,
`dislikes` and `shares` on local posts and comments, each also served at its own id."""
import pytest

from app import db
from app.activitypub.util import comment_model_to_json, post_to_page
from app.models import PostBoost
from tests.factories import make_instance, make_post_reply, make_user
from tests.test_ap_content_objects import ap_get, seed_local_post

SERVER = 'https://test.piefed.local'
```

---

### Task 1: Inline counts on posts and comments

**Files:**
- Modify: `app/activitypub/util.py` (`post_to_page` at ~312, `comment_model_to_json` at ~444; new helpers just above
  `post_to_page`)
- Test: `tests/test_ap_interop_count_collections.py` (create)

**Interfaces:**
- Produces:
  - `count_collection(collection_id: str, total: int) -> dict`
  - `post_shares_count(post_id: int) -> int`
  - `post_count_collections(post: Post) -> dict` with keys `'likes'`, `'dislikes'`, `'shares'`
  - `comment_count_collections(reply: PostReply) -> dict` with keys `'likes'`, `'dislikes'`

- [ ] **Step 1: Write the failing tests** (create the file with the shared header above, then:)

```python
def test_a_post_embeds_its_likes_dislikes_and_shares_counts(app, db_session):
    community, author, post = seed_local_post()
    post.up_votes, post.down_votes = 5, 2
    remote = make_instance('remote.example')
    for name in ('b1', 'b2', 'b3'):
        db.session.add(PostBoost(user_id=make_user(remote, name).id, post_id=post.id))
    db.session.commit()

    page = post_to_page(post)

    assert page['likes'] == {'id': f'{SERVER}/post/{post.id}/likes', 'type': 'Collection', 'totalItems': 5}
    assert page['dislikes'] == {'id': f'{SERVER}/post/{post.id}/dislikes', 'type': 'Collection', 'totalItems': 2}
    assert page['shares'] == {'id': f'{SERVER}/post/{post.id}/shares', 'type': 'Collection', 'totalItems': 3}


def test_a_post_with_null_cached_counts_embeds_zero(app, db_session):
    community, author, post = seed_local_post()
    post.up_votes, post.down_votes = None, None
    db.session.commit()

    page = post_to_page(post)

    assert (page['likes']['totalItems'], page['dislikes']['totalItems'], page['shares']['totalItems']) == (0, 0, 0)


def test_a_comment_embeds_likes_and_dislikes_but_no_shares(app, db_session):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    reply.up_votes, reply.down_votes = 3, None
    db.session.commit()

    note = comment_model_to_json(reply)

    assert note['likes'] == {'id': f'{SERVER}/comment/{reply.id}/likes', 'type': 'Collection', 'totalItems': 3}
    assert note['dislikes'] == {'id': f'{SERVER}/comment/{reply.id}/dislikes', 'type': 'Collection',
                                'totalItems': 0}
    assert 'shares' not in note


def test_no_inline_collection_carries_items(app, db_session):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    for collection in [*post_count_collections_of(post), *comment_count_collections_of(reply)]:
        assert set(collection) == {'id', 'type', 'totalItems'}


def post_count_collections_of(post):
    page = post_to_page(post)
    return [page['likes'], page['dislikes'], page['shares']]


def comment_count_collections_of(reply):
    note = comment_model_to_json(reply)
    return [note['likes'], note['dislikes']]
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py -q -p no:cacheprovider`
Expected: FAIL with `KeyError: 'likes'`.

- [ ] **Step 3: Implement** (in `app/activitypub/util.py`, above `post_to_page`)

```python
def count_collection(collection_id: str, total: int) -> dict:
    """A count-only collection as embedded in an object (interop C5, owner ruling): its id and `totalItems`, no
    items. Mastodon reads the inline `totalItems` of `likes` and `shares` without fetching; the route at `id` serves
    the same dict plus @context."""
    return {'id': collection_id, 'type': 'Collection', 'totalItems': total}


def post_shares_count(post_id: int) -> int:
    """How many boosts (Announces) `post_id` has received, from PostBoost."""
    return db.session.query(func.count(PostBoost.id)).filter(PostBoost.post_id == post_id).scalar()


def post_count_collections(post: Post) -> dict:
    """`likes`, `dislikes` and `shares` of local `post`, as post_to_page embeds them and post_count_collection_ap
    serves them. A NULL cached count is 0."""
    base = f"{current_app.config['SERVER_URL']}/post/{post.id}"
    return {'likes': count_collection(f'{base}/likes', post.up_votes or 0),
            'dislikes': count_collection(f'{base}/dislikes', post.down_votes or 0),
            'shares': count_collection(f'{base}/shares', post_shares_count(post.id))}


def comment_count_collections(reply: PostReply) -> dict:
    """`likes` and `dislikes` of local `reply`. No `shares`: comments have no boost records, and an inline 0 could
    make a peer overwrite its own count with 0."""
    base = f"{current_app.config['SERVER_URL']}/comment/{reply.id}"
    return {'likes': count_collection(f'{base}/likes', reply.up_votes or 0),
            'dislikes': count_collection(f'{base}/dislikes', reply.down_votes or 0)}
```

In `post_to_page`, directly after the `activity_data = {...}` literal closes:

```python
    activity_data.update(post_count_collections(post))
```

In `comment_model_to_json`, directly after the `reply_data = {...}` literal closes:

```python
    reply_data.update(comment_count_collections(reply))
```

- [ ] **Step 4: Run the new tests and every suite that calls the two serialisers**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py $(grep -rl "post_to_page\|comment_model_to_json" tests) -q -p no:cacheprovider`
Expected: PASS. If an existing test compares the WHOLE serialised dict, add the three (or two) new keys to its
expected value; do not weaken any other assertion.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py tests/test_ap_interop_count_collections.py
git commit -m "feat: local posts and comments embed count-only likes, dislikes and shares collections"
```

---

### Task 2: Group `following`

**Files:**
- Modify: `app/activitypub/util.py` (`community_actor_data` ~851), `app/activitypub/routes.py` (new route after
  `community_followers` ~2504)
- Test: `tests/test_ap_interop_count_collections.py`

**Interfaces:**
- Consumes: `count_only_collection(collection_id: str, total: int)` (routes.py, existing).
- Produces: route `GET /c/<actor>/following`, endpoint `activitypub.community_following`.

- [ ] **Step 1: Write the failing tests** (add `make_community` to the factories import)

```python
def test_the_group_document_names_its_following_collection(app, db_session):
    seed_local_post()   # seeds the site, instance and the local community 'books'

    assert ap_get(app, '/c/books').json['following'] == f'{SERVER}/c/books/following'


def test_a_group_follows_nothing(app, db_session):
    seed_local_post()

    response = ap_get(app, '/c/books/following')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == f'{SERVER}/c/books/following'
    assert response.json['totalItems'] == 0
    assert response.json['orderedItems'] == []


@pytest.mark.parametrize('flag', ['local_only', 'private'])
def test_a_local_only_or_private_groups_following_is_403(app, db_session, flag):
    community, author, post = seed_local_post()
    setattr(community, flag, True)
    db.session.commit()

    assert ap_get(app, '/c/books/following').status_code == 403


def test_an_unknown_or_remote_groups_following_is_404(app, db_session):
    seed_local_post()
    remote = make_community(name='films', host='remote.example')
    remote.ap_id = 'https://remote.example/c/films'
    db.session.commit()

    assert ap_get(app, '/c/nosuch/following').status_code == 404
    assert ap_get(app, '/c/films/following').status_code == 404


def test_a_browser_asking_for_a_groups_following_is_sent_to_the_group(app, db_session):
    seed_local_post()
    with app.test_client() as client:
        response = client.get('/c/books/following', headers={'Accept': 'text/html'})

    assert (response.status_code, response.headers['Location']) == (302, '/c/books')
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py -q -p no:cacheprovider`
Expected: the five new tests FAIL (`KeyError: 'following'`, 404s where 200/403 are expected).

- [ ] **Step 3: Implement**

In `community_actor_data`, after the `"followers": ...` entry:

```python
                  "following": f"https://{server}/c/{actor}/following",
```

In `routes.py`, after `community_followers`:

```python
@bp.route('/c/<actor>/following', methods=['GET'])
def community_following(actor):
    """Interop C5: a community follows nothing, so its following collection is always empty, count-only as the
    rest. Looked up and refused exactly as community_followers."""
    actor = actor.strip()
    if not is_activitypub_request():
        return redirect(url_for('activitypub.community_profile', actor=actor))
    community = Community.query.filter_by(name=actor, banned=False, ap_id=None).first() or abort(404)
    if community.local_only or community.private:
        abort(403)
    return count_only_collection(f'{current_app.config["SERVER_URL"]}/c/{actor}/following', 0)
```

- [ ] **Step 4: Run tests**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py $(grep -rl "community_actor_data\|'/c/books'" tests | head -20) -q -p no:cacheprovider`
Expected: PASS (update any whole-dict comparison of the Group document to include `following`).

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_ap_interop_count_collections.py
git commit -m "feat: a Group advertises a count-only following collection"
```

---

### Task 3: Person `liked`

**Files:**
- Modify: `app/activitypub/util.py` (new `user_liked_count` after `user_following_count` ~88),
  `app/activitypub/routes.py` (Person document ~516; new route after `user_following` ~2540; import
  `user_liked_count`)
- Test: `tests/test_ap_interop_count_collections.py`

**Interfaces:**
- Consumes: `local_actor_user(actor) -> User`, `count_only_collection(...)` (routes.py, existing).
- Produces: `user_liked_count(user: User) -> int`; route `GET /u/<actor>/liked`, endpoint `activitypub.user_liked`.

- [ ] **Step 1: Write the failing tests** (add `PostVote, PostReplyVote` to the models import; add `make_community`,
  `make_post` to the factories import)

```python
def _voter():
    community, author, post = seed_local_post()
    alice = make_user(None, 'alice', local=True)
    return community, author, post, alice


def _vote(user, post=None, reply=None, effect=1.0):
    if post is not None:
        db.session.add(PostVote(user_id=user.id, author_id=post.user_id, post_id=post.id, effect=effect))
    else:
        db.session.add(PostReplyVote(user_id=user.id, author_id=reply.user_id, post_reply_id=reply.id,
                                     effect=effect))
    db.session.commit()


def test_the_person_document_names_its_liked_collection(app, db_session):
    _voter()

    assert ap_get(app, '/u/alice').json['liked'] == f'{SERVER}/u/alice/liked'


def test_liked_counts_post_and_comment_upvotes_and_lists_none(app, db_session):
    community, author, post, alice = _voter()
    _vote(alice, post=post)
    _vote(alice, reply=make_post_reply(post, author))

    response = ap_get(app, '/u/alice/liked')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == f'{SERVER}/u/alice/liked'
    assert response.json['totalItems'] == 2
    assert response.json['orderedItems'] == []


def test_liked_skips_every_vote_that_was_not_federated(app, db_session):
    community, author, post, alice = _voter()
    _vote(alice, post=make_post(community, author, None, title='down'), effect=-1.0)
    for flag in ('local_only', 'private'):
        hidden = make_community(name=f'c_{flag}')
        setattr(hidden, flag, True)
        db.session.commit()
        _vote(alice, post=make_post(hidden, author, None, title=flag))
    followers_only = make_post(community, author, None, title='fo')
    followers_only.visibility = 'followers'
    deleted = make_post(community, author, None, title='gone')
    deleted.deleted = True
    db.session.commit()
    _vote(alice, post=followers_only)
    _vote(alice, post=deleted)
    hidden_reply = make_post_reply(post, author)
    hidden_reply.visibility = 'followers'
    deleted_reply = make_post_reply(post, author)
    deleted_reply.deleted = True
    db.session.commit()
    _vote(alice, reply=hidden_reply)
    _vote(alice, reply=deleted_reply)
    _vote(alice, reply=make_post_reply(post, author), effect=-1.0)

    assert ap_get(app, '/u/alice/liked').json['totalItems'] == 0


def test_a_user_who_votes_privately_has_liked_zero(app, db_session):
    community, author, post, alice = _voter()
    _vote(alice, post=post)
    alice.vote_privately = True
    db.session.commit()

    response = ap_get(app, '/u/alice/liked')

    assert response.json['totalItems'] == 0
    assert ap_get(app, '/u/alice').json['liked'] == f'{SERVER}/u/alice/liked'


def test_liked_answers_as_the_other_person_collections_for_gone_unknown_and_browser(app, db_session):
    community, author, post, alice = _voter()
    with app.test_client() as client:
        browser = client.get('/u/alice/liked', headers={'Accept': 'text/html'})
    assert (browser.status_code, browser.headers['Location']) == (302, '/u/alice')
    assert ap_get(app, '/u/nosuch/liked').status_code == 404
    alice.deleted = True
    db.session.commit()
    assert ap_get(app, '/u/alice/liked').status_code == 410
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py -q -p no:cacheprovider`
Expected: the new tests FAIL (`KeyError: 'liked'`, 404 for `/u/alice/liked`).

- [ ] **Step 3: Implement**

In `app/activitypub/util.py`, after `user_following_count`:

```python
def user_liked_count(user: User) -> int:
    """How many Likes local `user` has federated (interop C5, count-only): upvotes on posts and comments that
    send_vote would have sent -- the object is not deleted, its visibility is open and its community is neither
    local-only nor private. 0 for a user who votes privately, whose votes are never sent (owner ruling)."""
    if user.vote_privately:
        return 0
    posts = (db.session.query(func.count(PostVote.id))
             .join(Post, Post.id == PostVote.post_id)
             .join(Community, Community.id == Post.community_id)
             .filter(PostVote.user_id == user.id, PostVote.effect > 0, Post.deleted.is_(False),
                     Post.visibility.in_(OPEN_VISIBILITIES),
                     Community.local_only.isnot(True), Community.private.isnot(True))
             .scalar())
    replies = (db.session.query(func.count(PostReplyVote.id))
               .join(PostReply, PostReply.id == PostReplyVote.post_reply_id)
               .join(Community, Community.id == PostReply.community_id)
               .filter(PostReplyVote.user_id == user.id, PostReplyVote.effect > 0, PostReply.deleted.is_(False),
                       PostReply.visibility.in_(OPEN_VISIBILITIES),
                       Community.local_only.isnot(True), Community.private.isnot(True))
               .scalar())
    return posts + replies
```

In `routes.py`'s Person document, after `"following": ...`:

```python
                          "liked": f"{user.public_url()}/liked",
```

After `user_following`:

```python
@bp.route('/u/<actor>/liked', methods=['GET'])
def user_liked(actor):
    actor = actor.strip()
    # D177, as user_outbox above
    if not is_activitypub_request():
        return redirect(url_for('activitypub.user_profile', actor=actor))
    user = local_actor_user(actor)
    return count_only_collection(f"{user.public_url()}/liked", user_liked_count(user))
```

Add `user_liked_count` to the existing `from app.activitypub.util import ...` block in `routes.py` (where
`user_following_count` is imported).

- [ ] **Step 4: Run tests**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py tests/test_ap_interop_person_follow_collections.py tests/test_actor_profiles.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py app/activitypub/routes.py tests/test_ap_interop_count_collections.py
git commit -m "feat: a Person advertises a count-only liked collection of the Likes it federated"
```

---

### Task 4: Served post collections

**Files:**
- Modify: `app/activitypub/routes.py` (new helper `count_collection_response` and route after `post_replies_ap`;
  import `post_count_collections`)
- Test: `tests/test_ap_interop_count_collections.py`

**Interfaces:**
- Consumes: `post_count_collections(post) -> dict` (Task 1); `post_ap_refusal(post)`, `ap_content_headers(resp, obj)`,
  `ap_media_type()`, `default_context()` (existing).
- Produces: `count_collection_response(collection: dict, obj)` (Task 5 uses it); route
  `GET /post/<int:post_id>/<any(likes, dislikes, shares):collection>`, endpoint
  `activitypub.post_count_collection_ap`.

- [ ] **Step 1: Write the failing tests** (add `make_instance_block`, `make_post` to the factories import; add
  `from app.constants import POST_STATUS_REVIEWING`)

```python
POST_COLLECTIONS = ['likes', 'dislikes', 'shares']


@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_a_post_collection_serves_its_inline_object(app, db_session, name):
    community, author, post = seed_local_post()
    post.up_votes, post.down_votes = 4, 1
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/{name}')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    body = dict(response.json)
    assert body.pop('@context')
    assert body == post_to_page(post)[name]


def _post_local_only(community, post): community.local_only = True
def _post_private(community, post): community.private = True
def _post_unpublished(community, post): post.status = POST_STATUS_REVIEWING
def _post_followers_only(community, post): post.visibility = 'followers'
def _post_deleted(community, post): post.deleted = True


@pytest.mark.parametrize('refuse', [_post_local_only, _post_private, _post_unpublished, _post_followers_only,
                                    _post_deleted])
@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_a_post_collection_is_refused_exactly_as_its_post(app, db_session, refuse, name):
    community, author, post = seed_local_post()
    refuse(community, post)
    db.session.commit()

    assert ap_get(app, f'/post/{post.id}/{name}').status_code == ap_get(app, f'/post/{post.id}').status_code


@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_a_post_collection_is_401_when_the_author_blocked_the_requester(app, db_session, name):
    community, author, post = seed_local_post()
    make_instance_block(author, make_instance('blocked.example'))

    response = ap_get(app, f'/post/{post.id}/{name}', user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401


@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_an_unknown_or_remote_posts_collection_is_404(app, db_session, name):
    community, author, post = seed_local_post()
    remote = make_post(community, make_user(make_instance('remote.example'), 'far'),
                       'https://remote.example/p/1')

    assert ap_get(app, f'/post/999999/{name}').status_code == 404
    assert ap_get(app, f'/post/{remote.id}/{name}').status_code == 404


@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_a_browser_asking_for_a_post_collection_is_sent_to_the_post(app, db_session, name):
    community, author, post = seed_local_post()
    with app.test_client() as client:
        response = client.get(f'/post/{post.id}/{name}', headers={'Accept': 'text/html'})

    assert (response.status_code, response.headers['Location']) == (302, f'/post/{post.id}')
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py -q -p no:cacheprovider`
Expected: the new tests FAIL with 404 where other codes are expected.

- [ ] **Step 3: Implement** (after `post_replies_ap`)

```python
def count_collection_response(collection: dict, obj):
    """Serve the count-only collection an object embeds (interop C5), with the object's own cache headers."""
    resp = jsonify({'@context': default_context(), **collection})
    resp.content_type = ap_media_type()
    ap_content_headers(resp, obj)
    return resp


@bp.route('/post/<int:post_id>/<any(likes, dislikes, shares):collection>', methods=['GET'])
def post_count_collection_ap(post_id, collection):
    # GET only: Flask answers a HEAD itself (D192). Only a local post advertises these ids; a remote post's counts
    # live on its own server, and ours would be partial.
    if not is_activitypub_request():
        return redirect(url_for('activitypub.post_ap', post_id=post_id))
    post = db.session.get(Post, post_id) or abort(404)
    if not post.is_local():
        abort(404)
    refusal = post_ap_refusal(post)
    if refusal is not None:
        return refusal
    return count_collection_response(post_count_collections(post)[collection], post)
```

Add `post_count_collections` to the `from app.activitypub.util import ...` block.

- [ ] **Step 4: Run tests**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py tests/test_ap_content_objects.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_interop_count_collections.py
git commit -m "feat: a local post's likes, dislikes and shares collections are served at their ids"
```

---

### Task 5: `comment_ap_refusal` and served comment collections

**Files:**
- Modify: `app/activitypub/routes.py` (`comment_ap` ~2550; new `comment_ap_refusal` before it; new route after it;
  import `comment_count_collections`)
- Test: `tests/test_ap_interop_count_collections.py`

**Interfaces:**
- Consumes: `comment_count_collections(reply) -> dict` (Task 1); `count_collection_response(collection, obj)`
  (Task 4).
- Produces: `comment_ap_refusal(reply: PostReply)`; route
  `GET /comment/<int:comment_id>/<any(likes, dislikes):collection>`, endpoint `activitypub.comment_count_collection_ap`.

- [ ] **Step 1: Write the failing tests**

```python
COMMENT_COLLECTIONS = ['likes', 'dislikes']


@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_a_comment_collection_serves_its_inline_object(app, db_session, name):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    reply.up_votes, reply.down_votes = 2, 7
    db.session.commit()

    response = ap_get(app, f'/comment/{reply.id}/{name}')

    assert response.status_code == 200
    body = dict(response.json)
    assert body.pop('@context')
    assert body == comment_model_to_json(reply)[name]


def test_a_comment_has_no_served_shares(app, db_session):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    assert ap_get(app, f'/comment/{reply.id}/shares').status_code == 404


def _reply_local_only(community, reply): community.local_only = True
def _reply_private(community, reply): community.private = True
def _reply_followers_only(community, reply): reply.visibility = 'followers'
def _reply_deleted(community, reply): reply.deleted = True


@pytest.mark.parametrize('refuse', [_reply_local_only, _reply_private, _reply_followers_only, _reply_deleted])
@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_a_comment_collection_is_refused_exactly_as_its_comment(app, db_session, refuse, name):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    refuse(community, reply)
    db.session.commit()

    assert (ap_get(app, f'/comment/{reply.id}/{name}').status_code
            == ap_get(app, f'/comment/{reply.id}').status_code)


@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_a_comment_collection_is_401_when_the_author_blocked_the_requester(app, db_session, name):
    community, author, post = seed_local_post()
    make_instance_block(author, make_instance('blocked.example'))
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}/{name}', user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401


@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_an_unknown_or_remote_comments_collection_is_404(app, db_session, name):
    community, author, post = seed_local_post()
    remote = make_post_reply(post, make_user(make_instance('remote.example'), 'far'))
    remote.ap_id = 'https://remote.example/comment/1'
    db.session.commit()

    assert ap_get(app, f'/comment/999999/{name}').status_code == 404
    assert ap_get(app, f'/comment/{remote.id}/{name}').status_code == 404


@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_a_browser_asking_for_a_comment_collection_is_sent_to_the_comment(app, db_session, name):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    with app.test_client() as client:
        response = client.get(f'/comment/{reply.id}/{name}', headers={'Accept': 'text/html'})

    assert (response.status_code, response.headers['Location']) == (302, f'/comment/{reply.id}')
```

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py -q -p no:cacheprovider`
Expected: the new tests FAIL (404 where 200/302/401 are expected). `test_a_comment_has_no_served_shares` may
already pass; that is fine.

- [ ] **Step 3: Implement**

Before `comment_ap`:

```python
def comment_ap_refusal(reply: PostReply):
    """The response that refuses an ActivityPub fetch of local `reply` (or of its count collections), or None to
    serve it. comment_ap's checks, in their order, held here so the comment and its collections cannot diverge."""
    if reply.community.local_only or reply.community.private:
        abort(403)
    if reply.visibility not in OPEN_VISIBILITIES and not followers_entitled(reply):
        abort(404)
    if reply.deleted:
        return tombstone_response(reply.ap_id, 'Note', private=not is_open(reply))
    requesting_domain = signed_requestor_domain()
    if reply.author.has_blocked_instance(known_instance_id(requesting_domain)):
        return make_response(f'Author has blocked {requesting_domain}'), 401
    return None
```

In `comment_ap`, replace the four checks after `if not reply.is_local(): return redirect(...)` (from
`if reply.community.local_only or reply.community.private:` through the `return make_response(...), 401` line) with:

```python
        refusal = comment_ap_refusal(reply)
        if refusal is not None:
            return refusal
```

After `comment_ap`:

```python
@bp.route('/comment/<int:comment_id>/<any(likes, dislikes):collection>', methods=['GET'])
def comment_count_collection_ap(comment_id, collection):
    # GET only (D192). Only a local comment advertises these ids, as post_count_collection_ap.
    if not is_activitypub_request():
        return redirect(url_for('activitypub.comment_ap', comment_id=comment_id))
    reply = db.session.get(PostReply, comment_id) or abort(404)
    if not reply.is_local():
        abort(404)
    refusal = comment_ap_refusal(reply)
    if refusal is not None:
        return refusal
    return count_collection_response(comment_count_collections(reply)[collection], reply)
```

Add `comment_count_collections` to the `from app.activitypub.util import ...` block.

- [ ] **Step 4: Run tests, including every existing comment_ap test unchanged**

Run: `./run_tests.sh tests/test_ap_interop_count_collections.py tests/test_ap_content_objects.py $(grep -rl "/comment/" tests | grep -i "ap_\|visib\|secure\|tombstone" | head -20) -q -p no:cacheprovider`
Expected: PASS with no edits to existing comment tests.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_ap_interop_count_collections.py
git commit -m "feat: a local comment's likes and dislikes collections are served at their ids"
```

---

## Final gate (controller)

```
./run_tests.sh tests/ -q -p no:cacheprovider --cov=app --cov-branch --cov-report=json:coverage.json
python3 tests/check_changed_line_coverage.py coverage.json <base> --branches
python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

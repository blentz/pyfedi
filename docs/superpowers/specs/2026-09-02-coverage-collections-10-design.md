# Sub-project 10: the ActivityPub collection endpoints

**Date:** 2026-09-02
**Module:** `app/activitypub/routes.py` — `community_outbox`, `community_featured`,
`community_moderators_route`, `community_followers`, `user_followers`, `feed_outbox`,
`feed_following`, `feed_moderators_route`, `feed_followers`
**Predecessors:** 5a-7 covered the inbox dispatcher and its delegates; 8 the
webfinger discovery surface; 9 the actor-profile endpoints. This is the third
slice of the outbound side, and the last large one.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D167**

## The unit

Eight endpoints, taken together because they are eight near-identical answers to
one question — *list the things attached to this actor* — and because, as in
sub-projects 7, 8 and 9, the defects live in what they do **differently**.

| Function | Lines at writing | Uncovered / total |
|---|---|---|
| `feed_following` | 2776-2808 | **18 / 19** |
| `feed_outbox` | 2739-2772 | **16 / 17** |
| `community_outbox` | 2013-2040 | **15 / 16** |
| `user_followers` | 2115-2143 | **14 / 15** |
| `community_moderators_route` | 2069-2091 | **13 / 14** |
| `feed_moderators_route` | 2812-2837 | **13 / 14** |
| `community_featured` | 2044-2065 | **11 / 12** |
| `community_followers` | 2095-2111 | **9 / 10** |
| `feed_followers` | 2841-2861 | **11 / 12** |

**120 uncovered statements**, and all nine sit between 4% and 10% covered. They
are how a remote instance enumerates a community's posts, a user's followers, a
feed's communities and every actor's moderators.

The count is larger than sub-project 9's 96, but the work is not: the eight share
one harness, one seeding surface and one response shape. The marginal cost after
the first endpoint is small.

### Citations drift

Every sub-project since 5c found its brief's line numbers stale, and this
slice's own fixes will move things. **Locate every target by code content.**
Quoted lines are navigational aids, never identifiers.

## What they do

All eight share a skeleton: strip the actor, resolve it, build a JSON collection
document, set `content_type` and usually `Cache-Control`, return. Where they
differ is the whole point.

| Axis | community (3) | user (1) | feed (4) |
|---|---|---|---|
| `'@'` remote check | **none** | **none** | `abort(400)` |
| Lookup guards | `name, banned=False, ap_id=None` | `user_name, banned=False, ap_id=None` | `name.lower(), ap_id=None` — **no ban, no public** |
| Not-found path | `abort(404)` | `abort(404)` | **three of four are broken — see below** |
| Visibility guard | none | none | `if not feed.public: abort(403)` on two of four |
| Collection type | `OrderedCollection` | `Collection` | `Collection` ×3, `OrderedCollection` ×1 |
| `Cache-Control` | 10 / **none** / 120 | 15 | 5 / 10 / 10 / 15 |
| `Vary: Accept` | none | **yes** | none |
| Items populated | yes | yes | yes ×3, **`[]` always** ×1 |

`is_activitypub_request()` is checked by **none** of the eight — unlike the
three actor-profile endpoints, which all branch on it. These return ActivityPub
JSON to a browser.

## Goal

Full statement coverage of all eight, and branch coverage sufficient that no
guard survives having any one of its conjuncts dropped. Expected effect:
`app/activitypub/routes.py` moves from its measured **80.3676%** blended toward
**86%**, and the floor rises from 80 to the measured figure rounded down.

## Out of scope

The content-object endpoints (`post_ap` 24 uncovered, `post_ap_context` 21,
`comment_ap` 16, `post_replies_ap` 12) are the natural next slice.
`process_delete_request` (21) is a `@celery.task` belonging with the inbox work;
`announce_activity_to_followers` (19) is outbound delivery, not a route;
`lemmy_federated_instances` (17) is a cached API endpoint.

The delegates these call — `post_to_activity`, `post_to_page`,
`community_moderators`, `default_context` — are doubled or driven with real
rows here, not tested here.

## The defects this slice must confront

This slice carries the same bounded fix authorisation 5c through 9 had:
**defects found inside these eight functions are fixed test-first, each in its
own commit, separate from every test-only commit, and each proved by a mutation
that fails a named test.** Anything larger is registered.

### Three feed collections return HTTP 500 for an unknown feed

This is the headline defect, and it is reachable by any remote instance.

`feed_outbox` and `feed_following` both resolve the feed and then use it
without ever checking for `None`:

```python
feed: Feed = db.session.query(Feed).filter_by(name=actor.lower(), ap_id=None).first()

if not feed.public:      # AttributeError when feed is None
    abort(403)
```

`feed_moderators_route` has the check but no `else`:

```python
if feed is not None:
    ...
    return resp
# falls off the end -> returns None -> Flask raises
```

So `/f/nosuchfeed/outbox`, `/f/nosuchfeed/following` and
`/f/nosuchfeed/moderators` are **500s**, where every community and user
collection — and `feed_followers`, the fourth of the four — returns 404.
`feed_followers` shows the correct shape a few lines below its broken siblings.

### `feed_outbox` and `feed_following` build a cartesian product

Both do:

```python
db.session.query(FeedItem).join(Feed, FeedItem.feed_id == feed.id)
```

The `ON` condition does not mention the joined `Feed` table at all — it compares
`FeedItem.feed_id` to an already-resolved constant. So every matching `FeedItem`
is paired with **every row in `feed`**, and the collection lists each community
once per feed that exists on the instance. `totalItems` inflates with it. The
intended query is a filter, not a join.

Whether the duplication is observable depends on how many feeds exist, which is
exactly what a test can settle.

### `feed_outbox` leaks what `feed_following` hides

`feed_outbox`'s own comment says it "will just be the same as the /following
collection". It is not. `feed_following` skips communities that are `local_only`
or `private`; `feed_outbox` has no such filter, and uses `c.ap_public_url` where
`feed_following` uses `c.public_url()`. So the endpoint documented as equivalent
publishes the URLs of communities its twin deliberately withholds.

### `community_featured` ignores post status

`community_outbox` filters `Post.status > POST_STATUS_REVIEWING`;
`community_featured` filters only `deleted=False`. A sticky post still under
review is therefore excluded from the outbox and published in the featured
collection.

### `community_featured` sets no `Cache-Control` at all

Its seven siblings set one, ranging from 5 to 120 seconds with no evident
rationale for any particular value. `community_featured` sets none, so caching
falls to whatever default the deployment applies.

### Also present, registered rather than fixed unless the plan argues otherwise

- **`totalItems` is the page size, not the collection size**, in
  `community_outbox`: `posts` is capped at 50 and `totalItems` is `len(posts)`.
  A remote instance paginating on that number sees a collection that claims to
  be complete at 50. Correcting it changes what peers are told, so it is
  registered by default.
- **Two of the three followers collections report a real `totalItems` with
  `items` permanently `[]`** — `community_followers` and `feed_followers`.
  `user_followers`, the third, populates its items with real follower URLs and
  filters blocked and unaccepted follows. Hiding follower lists is a defensible
  privacy choice, but reporting a non-zero count beside an empty list is
  self-contradictory: a consumer cannot tell "hidden" from "none". That two of
  three do it and one does not is the asymmetry.
- **The feed lookups have neither a `banned` nor a `public` guard**, while the
  community and user lookups both filter `banned=False`. Two of the four feed
  endpoints check `feed.public` after the fact; two do not check it at all.
- **None of the eight checks `is_activitypub_request()`**, so all eight return
  ActivityPub JSON to a browser, unlike the three actor-profile endpoints.
- **Only `user_followers` sets `Vary: Accept`** — and it is the one endpoint
  whose body does *not* vary by `Accept`, since none of them negotiate.
- **`user_followers` guards on `user.ap_followers_url` being truthy**, so a
  local user without that column populated 404s rather than returning an empty
  collection.

## Testing approach

**Entry.** `app.test_client()`, driving each endpoint's URL. This is the harness
sub-projects 8 and 9 proved; no new infrastructure is needed.

**Reuse sub-project 9's helpers rather than rebuilding them.**
`tests/test_actor_profiles.py` already has `profile_get`, `seed_actors`,
`_seed_file` and `_double_the_renderers`. The plan decides whether to import
them, copy the two that apply, or factor them into a shared module — but it
must not reinvent them silently.

**Seeding.** `make_community`, `make_user`, `make_local_feed`,
`make_community_member`, `seed_community_owner` and `make_post` all exist.
`FeedItem`, `FeedMember`, `UserFollower` and `UserBlock` rows have no factories
yet; the plan confirms what each needs against the models rather than assuming,
and adds factories only where a raw row would be repeated across tasks.

**The 500s are the most valuable tests in this slice.** Three endpoints raise
where they should 404. Those tests pin real, remotely-reachable crashes, and
they are cheap: request an actor that does not exist.

**Every test asserts `response.status_code`**, and success paths also assert
`content_type` and `Cache-Control` where the endpoint sets one. `community_featured`
sets none — assert that absence, since it is a registered defect.

**No vacuous assertions.** `Community.banned`, `User.banned`, `Feed.public` and
`Feed.banned` all default to `False`; `Post.deleted` and `Post.sticky` likewise.
Every test that depends on one sets it explicitly.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped separately, each killed by a distinct named test. Carry forward the
patterns this campaign has now hit repeatedly:

- **A filter clause whose value equals what the factory always produces cannot
  be killed** by any test using that factory unmodified. All the local lookups
  filter `ap_id=None` and every local-actor factory produces exactly that.
- **A guard behind an earlier `abort` is never reached**, so its mutation kills
  nothing — sub-project 9 hit this with `feed_profile`'s remote lookup.
- **A mutation stripping a clause from two call sites at once proves the column,
  not the query.** Mutate one site at a time.

**Docstrings must be true**, including after any fix. After inverting a pin,
check which branch that pin used to cover.

**`Vary` is never absent and never bare** — Flask-Compress appends
`Accept-Encoding` to every response.

**A test cannot log in after making an earlier request in the same test** —
Flask-Login caches on `g`, and the `app` fixture pushes one application context
per test. None of these endpoints reads `current_user`, so this should not
arise; it is recorded because it silently defeated a pin in sub-project 9.

**One pytest session at a time.**

## New test file

One new file, `tests/test_ap_collections.py`. Eight endpoints across ~110
statements is one coherent unit sharing a harness, and splitting it by actor
type would hide exactly the cross-type comparisons this slice exists to make.

No new factories are assumed. **The plan confirms this against the models**,
rather than repeating sub-project 8's spec, which claimed no factory was needed
and was wrong.

## Global constraints

- Defects found in these eight functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered.
- Findings are numbered from **D167**, and **both** of the register's "Next free
  number" notes are updated in the same change.
- The coverage floor rises to the measured blended figure rounded down.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a time,
  and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` and
  `scratch_full_cov.json` in the repository root are not the campaign's.

## Success criteria

1. All eight functions reach full statement coverage.
2. No guard survives any one conjunct being dropped, each kill by a distinct
   named test.
3. Every test asserts the status code; success paths also assert the response
   headers that form the federation contract.
4. The three unknown-feed 500s are pinned, then fixed or registered with a
   stated reason.
5. Every asymmetry in the table above is either fixed with a mutation-proved
   test or registered with a stated reason.
6. `coverage_floors.ini` raised for `app/activitypub/routes.py`.
7. The findings register carries every defect found, from D167.
8. Full suite green.

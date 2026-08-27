# Microblog Boost Ingestion (`process_microblog_announce`)

Date: 2026-08-23
Status: Approved design, ready for implementation planning

## Problem

PieFed cannot ingest boosts (`Announce` activities) from microblogging platforms
such as Mastodon. When a PieFed user follows a Mastodon account and that account
boosts a post, the activity reaches the shared inbox, is dispatched to
`process_microblog_announce()` in `app/activitypub/util.py:3418`, and is silently
discarded. The function fetches the remote object and then falls off the end of its
body, returning `None`.

The effect is that following a Mastodon account gives a PieFed user only that
account's original posts. Everything the account amplifies is lost, which is a
large fraction of typical microblog activity.

## Current state of the code

The scaffolding for this feature already exists and is entirely unused:

- `PostBoost` model — `app/models.py:4255` — columns `id`, `user_id`, `post_id`,
  `created_at`. Written and read nowhere in the codebase.
- `Post.post_boosts` JSON column — `app/models.py:1675` — commented as "a cache of
  the boosts(retweets) a microblog post has received, to avoid joins". Never populated.
- `Post.boosts` relationship — `app/models.py:1698`.
- Migration `migrations/versions/c831b9c7eee9_post_boost.py`, added in commit
  `9034b925`.
- The call site is already wired: `app/activitypub/routes.py:868`, reached when an
  `Announce` arrives whose object is a string URI and whose actor did not resolve to
  a local Community.

The stub itself was added in commit `5e4becab` with no body. The explanatory
docstring was added later in `82ce9804`.

### Upstream status

`origin` is the upstream repository (`codeberg.org/rimu/pyfedi`). As of
2026-08-23, `origin/main` carries the byte-identical unimplemented stub, and
`PostBoost` remains unreferenced there. No open pull request touches this code.

Related open issues on the tracker, none of which implement this:

- [#1872](https://codeberg.org/rimu/pyfedi/issues/1872) — "Mastodon: add subscribed
  filter to c/microblog", asking that microblog content be limited to followed
  accounts. This overlaps with the trust gate described below but applies to the
  `Create` path, which is out of scope here. The `announcer_is_followed()` helper
  introduced by this work is written so that issue can reuse it.
- [#1875](https://codeberg.org/rimu/pyfedi/issues/1875) — microblog posts are hard
  to open in compact mode because they have no title.
- [#1874](https://codeberg.org/rimu/pyfedi/issues/1874) — image crossposts from
  microblog content.

## Scope

### In scope

1. Ingest boosts of top-level microblog posts from accounts a local user follows.
2. Record boosts of posts that already exist locally, including posts authored on
   this instance.
3. Handle `Undo`/`Announce` so un-boosts remove the recorded boost.
4. Surface boosts by followed accounts in the feed.

### Out of scope

The following were considered and deliberately excluded. Each is recorded with its
reason so a later project does not have to rediscover it.

- **Thread backfill.** If a boosted object is a reply whose ancestors are absent
  locally, it is dropped. Walking `inReplyTo` upward, or materialising a `context`
  collection, means unbounded attacker-controlled recursion against untrusted hosts
  and needs its own design.
- **Boosts of replies.** `PostBoost` has no `post_reply_id` column. Adding one is a
  migration whose data nothing would read, because feed surfacing is post-shaped.
  A boost whose object resolves to a `PostReply` is logged and ignored.
- **The `Create` path trust gate.** Issue #1872 wants unfollowed accounts filtered
  out of the microblogs community generally. That changes existing behaviour for
  content already being ingested and belongs in its own change.
- **Outbound boosting.** PieFed users cannot boost, and this work does not add it.

## Architecture

### Control flow

`process_microblog_announce(request_json, id, store_ap_json) -> Post | None`. The
return type and signature are unchanged so the call site at
`app/activitypub/routes.py:868` keeps its existing contract.

1. **Trust gate, before any network I/O.** Resolve the announcing actor from
   `request_json['actor']` using `find_actor_or_create_cached`. Proceed only if a
   `UserFollower` row exists with `remote_user_id == actor.id` and
   `is_inward == False` — that is, at least one local user follows the announcer.
   Otherwise log `APLOG_ANNOUNCE` / `APLOG_IGNORED` with reason
   `'Announce from unfollowed actor'` and return `None`.

   The ordering is the security property. Performing this check before the fetch
   means no unauthenticated remote party can cause PieFed to issue an outbound
   request to a URL of their choosing.

2. **Local lookup first.** `Post.get_by_ap_id(uri)`. Local posts carry a full
   `ap_id` assigned by `Post.generate_ap_id()` (`app/models.py:2393-2401`), so this
   resolves both remote posts already ingested and posts authored on this instance.
   On a hit, skip directly to step 6 — no fetch, no creation.

3. **Fetch.** `remote_object_to_json(uri)` (`app/activitypub/util.py:3621`). It
   already retries a 401 with a signed GET, which covers instances running in
   authorized-fetch mode. On `None`, log failure and return.

4. **Reject non-top-level objects.** If the fetched object has a truthy
   `inReplyTo`, log `APLOG_ANNOUNCE` / `APLOG_IGNORED` with reason
   `'Boosted object is a reply'` and return `None`. This is the single place the
   top-level-only decision is enforced.

5. **Create.** Delegate to
   `create_resolved_object(uri, post_data, uri_domain, find_microblogging_community(),
   announce_id=id, store_ap_json)` (`app/activitypub/util.py:3678`). That function
   already performs the `attributedTo` parsing and the actor-domain-matches-object-domain
   impersonation check at lines 3680-3702. This check must not be reimplemented
   here; two copies of a security check is how one of them rots.

   Passing the Announce id as `announce_id` sets `Post.ap_announce_id` the same way
   the community path does.

   If the return value is a `PostReply` rather than a `Post`, log ignored and return
   `None`. Step 4 should have prevented this, but the guard keeps the contract
   honest.

6. **Record the boost.** `record_boost(post, announcer)`, described under Data below.
   Return the `Post`.

### Rejected alternatives

- **Bespoke ingestion.** Calling `create_post` directly from
  `process_microblog_announce` would mean re-implementing the `attributedTo` parsing
  and the domain-match guard from `create_resolved_object`. Rejected to avoid
  duplicating a security check.
- **Synthesising a `Create` and re-entering `process_new_content`.** This would
  reuse more logic, but `process_new_content` returns nothing, so linking the
  resulting post to a `PostBoost` would require re-querying by `ap_id`. It also
  calls `announce_activity_to_followers`, which would rebroadcast microblog content
  to community followers. Suppressing that means threading a new parameter through a
  hot path. Rejected.

## Data model and idempotency

No schema change. The existing `PostBoost` table and `Post.post_boosts` cache are
used as originally built.

**`record_boost(post, user)`** queries for an existing `PostBoost` row with the same
`user_id` and `post_id`, and inserts only if absent. Idempotency is enforced by
query-then-insert rather than a unique constraint, because adding a constraint means
a migration plus a dedupe of existing rows, and the table is empty on every
deployment today. This matters because the duplicate suppression at
`app/activitypub/routes.py:647` is a Redis key with a 90-second expiry — replay
protection, not durable dedup. Redelivery after that window must not double-count.

**`Post.update_boost_cache()`** is a new method on `Post`, modelled directly on
`Post.update_reaction_cache()` (`app/models.py:2694`). It rebuilds `post_boosts`
from the `PostBoost` rows on every change. Entries have the shape:

```python
{"user_id": int, "ap_id": str, "display_name": str, "created_at": str}
```

so a post page can render who boosted it without a join.

## Undo / un-boost

There is currently no `Undo`/`Announce` branch. The `Undo` chain at
`app/activitypub/routes.py:1645` onward handles only `Follow`, `Delete`,
`Like`/`Dislike`, `Lock`, `Block`, and `ChooseAnswer`. Without this, boost counts
only ever increase.

A new branch is added after the `Like`/`Dislike` branch:

- The actor is the already-resolved `user`, which comes from the signed
  `request_json['actor']` at `app/activitypub/routes.py:822`, resolved at line 838.
  The actor must never
  be read from the inner object; the comment at `app/activitypub/routes.py:819`
  documents that spoofing hole.
- The boosted object URI is `core_activity['object']['object']`, accepting either a
  string or a dict whose `id` is taken.
- `Post.get_by_ap_id(uri)`. If there is no such post, log ignored and return. An
  `Undo` never triggers a fetch, because there is nothing to create.
- Delete the matching `PostBoost` row if present, then call
  `post.update_boost_cache()`. A missing row is a successful no-op, not a failure —
  Mastodon re-sends activities.
- No trust gate applies. Removing a boost is always safe, and gating it could strand
  rows if the announcer is unfollowed between the boost and the un-boost.

## Boosts of local posts

`app/activitypub/routes.py:864-866` returns early for any `Announce` whose object
URL starts with this server's name, before the `community is None` dispatch at line
868. A followed Mastodon account boosting a post authored on this instance therefore
never reaches `process_microblog_announce`, and no boost is recorded. This is likely
the most common boost a PieFed instance receives.

The fix is to test `community is None` before that early return, and to rely on step
2 of the control flow — the local lookup — to short-circuit to `record_boost`
without fetching or creating anything.

The early return remains in place for the `community is not None` path, so
community-path `Announce` deduplication is unchanged. Because this touches a shared
branch, the implementation plan must include an explicit regression check that
community `Announce` handling still discards duplicates of local content.

## Feed surfacing

Without this, ingestion is invisible bookkeeping. The followed-users clause in
`app/utils.py:3253-3257` currently matches only on the post's author:

```sql
EXISTS (SELECT 1 FROM user_follower uf
        WHERE uf.local_user_id = :local_user_id
        AND uf.remote_user_id = p.user_id AND is_inward is false)
```

A second `EXISTS` is added, over `post_boost` joined to `user_follower` on
`uf.remote_user_id = post_boost.user_id`, so posts boosted by followed accounts
appear alongside posts authored by them. Both `post_boost.post_id` and
`post_boost.user_id` are already indexed by the existing migration.

This sits in the hot feed path. The implementation plan gates the change on an
`EXPLAIN ANALYZE` of the feed query before and after, against a populated database.

## Error handling and logging

- Every exit path calls `log_incoming_ap` with a distinct reason string. The current
  stub's bare `return None` is precisely what makes it undiagnosable in production.
- At most one fetch of the boosted object per activity, and only after the trust gate
  passes. This is deliberately not a claim of zero I/O after the gate:
  `create_resolved_object` calls `find_actor_or_create`, which may fetch the
  `attributedTo` actor's profile. That fetch is bounded and acceptable — it happens
  behind the gate, and the domain-match check rejects any `attributedTo` on a
  different host than the object, so a sender cannot use it to reach a host of their
  choosing. Enforcing a literal single fetch would mean resolving actors with
  `create_if_not_found=False`, which would drop boosts of authors this instance has
  never seen.
- No retry loop is added. `process_inbox_request` already runs as a Celery task; a
  fetch failure logs and drops the activity.

## Security considerations

- **Content injection.** The trust gate confines ingestion to accounts a local user
  follows, so a stranger who can sign a request cannot inject posts into the
  microblogs community, which is visible in the "All" feed.
- **Outbound request control.** Because the gate runs before the fetch, an untrusted
  party cannot use an `Announce` to make PieFed request an arbitrary URL.
- **Impersonation.** The object's host must match its `attributedTo` host, enforced
  by the existing check in `create_resolved_object`.
- **Actor spoofing on Undo.** Only the HTTP-signature-verified outer actor is used.

## Testing

`tests/test_activitypub_util.py` requires a live database, live network access, and
a hardcoded username, so it is not a suitable home for this coverage. Rather than
verify this work manually, the implementation builds the missing test
infrastructure first.

**Test fixtures.** `tests/conftest.py` provides a session-scoped `app` bound to a
throwaway database named by `TEST_DATABASE_URL`, and a function-scoped `db_session`
that truncates every table after each test. Database-backed tests skip, rather than
fail, when that variable is unset, so a bare checkout can still run the
pure-function tests. Schema is created by `flask db upgrade`, never
`db.create_all()`: SQLAlchemy-Searchable triggers and several indexes are created by
migrations and would otherwise be absent, meaning tests would exercise a different
schema than production. `tests/factories.py` provides row builders.

Remote fetches are stubbed by monkeypatching `remote_object_to_json`. No HTTP
mocking dependency is added.

**Testability shapes the design.** Two pieces of inbox logic move out of
`app/activitypub/routes.py` and into `app/activitypub/util.py` so they can be tested
without Redis or signature plumbing, following the existing `undo_vote()` precedent
at `app/activitypub/util.py:3209`:

- `process_announce_of_uri(request_json, community, id, store_ap_json)` — the
  dispatch for an `Announce` whose object is a bare URI, including the local-content
  ordering described above.
- `undo_boost(target_ap_id, user)` — returns the post whose boost was removed.

**Pure helpers**, table-driven tests, no application context:

- `announce_target_uri(activity) -> str | None`
- `is_top_level(post_data) -> bool`
- `boost_cache_entries(rows) -> list` — split out of `update_boost_cache` so the
  stored JSON shape is testable without a database.

**Database-backed tests:**

- `record_boost` idempotency, `remove_boost` no-op on a missing row, per-user
  isolation, and cache contents.
- Trust gate: an unfollowed or banned announcer is rejected, asserted together with
  a call counter proving **no fetch occurred**. This is how the security property is
  enforced, rather than by reading the code.
- A boosted reply is ignored, after exactly one fetch.
- A boost of a post already held locally short-circuits without fetching.
- Redelivery of the same `Announce` records one boost.
- Dispatch: microblog boost of local content is recorded; community-path `Announce`
  of local content is still discarded; community-path `Announce` of remote content
  still resolves.
- `Undo`: removes the row, returns the post, is a no-op when repeated, and leaves
  other users' boosts intact.
- Feed clause: executed as SQL against seeded data, plus a guard asserting the tested
  SQL matches what `get_deduped_post_ids` actually builds.

**Not automated:**

- `EXPLAIN ANALYZE` of the feed query before and after the `EXISTS` addition, on a
  populated database. This is a merge gate.
- One end-to-end check that a boosted post appears in the running app's feed.

## Risks

- The feed query change is the main performance risk, and is the reason for the
  `EXPLAIN ANALYZE` gate.
- Reordering the early return at `app/activitypub/routes.py:864` touches a branch
  shared with the community path. Covered by the regression check above.
- The local branch is 111 commits ahead of and 109 behind `origin/main`. This work
  should be rebased onto current `origin/main` before implementation, since the
  target functions have moved before.

# ActivityPub relay support

Status: design approved in session (2026-10-08). Fork-only work. It follows the Live feed work
(`2026-10-07-microblog-live-feed-design.md` and its Amendment A).

## Problem

The Live views work, but they are quiet. Measured on hell.cloud on 2026-10-08 at 02:36Z:

| Feed | Posts in the last hour |
|---|---|
| `/c/microblogs` | 9 (about 18/h averaged over 5.5 h) |
| home All | 51 |
| home Subscribed | 60 |

A mid-sized Mastodon server's Live Feed gets several posts a second. That volume comes from two
sources: the follows of a large user base, and **ActivityPub relays**, which push public posts from
many instances to every subscriber. PieFed has no relay support. `grep -i relay app/` finds only
Lemmy community re-announcing and SMTP.

## Goals

- An admin can subscribe hell.cloud to relays of both common styles:
  - **Mastodon-style** relays, which forward the author's own LD-signed activity.
  - **LitePub/Pleroma-style** relays, including FediBuzz hashtag and instance relays. These send an
    `Announce` of a post URI as the relay.
- Relayed public top-level posts are stored through the normal ingest gates and appear in
  `/c/microblogs` and its Live view.
- Storage stays bounded: relayed posts nobody here engaged with expire after N days.

**Success:** with one busy relay subscribed, hell.cloud receives tens to hundreds of public posts a
minute, and they stream into `/c/microblogs` Live. With no relays subscribed, behavior is unchanged.

**Non-goals:**
- per-user relay subscriptions
- running hell.cloud as a relay for others
- fetching unknown reply threads
- relayed boosts
- letting microblog posts into the home All/Local/Popular/Media tabs (`MICROBLOG_GATE` is unchanged)
- a per-relay rate cap (removing the relay is the off switch)

## Decisions (owner rulings, 2026-10-08)

1. **Styles:** both Mastodon-style and LitePub-style relays.
2. **Kept content:** top-level posts only.
   - A relayed toot with no community goes to `/c/microblogs`.
   - A relayed post addressed to a community hell.cloud already has goes to that community.
   - A post addressed to an unknown community is dropped. Relays never create communities.
   - A relayed reply is kept only if its parent is already stored.
   - Relayed boosts are dropped.
3. **Retention:** relayed posts expire after `relay_retention_days` (default 7) unless engaged (see
   Expiry).
4. **Approach:** a dedicated `Relay` table, an admin page and a CLI, the existing instance actor as
   the subscriber, and `Post.relay_id` marking relayed posts. This rules out reusing
   `DiscoverySync`, whose rows are keyed to a community, and rules out a Settings list.

## Existing code this builds on

- **Instance actor `/actor`:** `app/main/routes.py:1293`. Type `Application`, key id
  `/actor#main-key`, signed with the Site key. `/actor/inbox` delegates to `shared_inbox`.
- **Signed Follow and Undo from the instance actor:** `app/discovery/instance_actor.py`
  (`follow_activity`, `send_instance_follow`, `send_instance_undo`, `_signing`).
- **Accept/Reject routing to the instance actor:** `app/activitypub/routes.py:~1195` and `~1288`, then
  `app/discovery/instance_answers.py` (`instance_actor_answer`, `record_answer`,
  `record_delivery_refusal`).
- **Shared inbox verification:** `app/activitypub/routes.py:698-818`.
  - The HTTP signature is verified against the *activity actor's* key (`verify_request` never reads
    `keyId`).
  - It falls back to the LD signature (`LDSignature.verify_signature`).
  - Mastodon-style relayed Creates already pass through that fallback.
- **Microblog announce path:** `process_microblog_announce` (`app/activitypub/util.py:4825`). Its gate
  "announcer must be followed by a local user" is at `:4855`.
- **Create routing:**
  - `process_new_content` (`app/activitypub/routes.py:~2640`), then `find_microblogging_community`.
  - `can_create_post` (`app/utils.py:2829`) and `create_post` (`app/activitypub/util.py:3264`).
- **Ingest gates:** `instance_banned`, `instance_allowed`, the allowlist and `ALLOWLIST_INTENSE`.
- **Background Celery queue:** the `celery_background` worker from commit 261ee07b1.
- **Admin:**
  - The federation admin is in `app/admin/routes.py`.
  - The discovery admin view (`app/discovery/admin_views.py`) and CLI (`app/discovery/cli.py`) are the
    closest templates.

## Data

A migration with a reversible downgrade adds the following. It must not lock `post`, the largest table,
for a scan or an index build: it sets `lock_timeout = '10s'`, adds `post.relay_id` as a nullable column
with no default, creates its foreign key `NOT VALID` (the column is all NULL, so there is nothing to
validate), and builds `ix_post_relay_id` `CONCURRENTLY` inside an autocommit block. The downgrade drops
the index concurrently the same way. The model declares the same index and the foreign key name
`fk_post_relay_id`.

**Table `relay`:**

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `url` | String(2048), unique, not null | what the admin entered |
| `style` | String(16), not null | `mastodon` or `litepub` |
| `inbox_url` | String(2048), not null | where the Follow is sent |
| `actor_id` | String(2048), nullable | the relay actor's id; null for a Mastodon-style relay whose actor is unknown |
| `public_key` | Text, nullable | the relay actor's key, used to verify its HTTP signatures |
| `follow_activity_id` | String(2048), not null | the id of our Follow, used to match the Accept or Reject |
| `state` | String(16), not null | `pending`, `accepted`, `refused` or `failed` |
| `created_at` | DateTime, not null | |
| `answered_at` | DateTime, nullable | |
| `last_error` | String(1024), nullable | |

**Column `post.relay_id`:** an integer foreign key to `relay.id`, nullable, `ON DELETE SET NULL`, with
the partial index `ix_post_relay_id` `WHERE relay_id IS NOT NULL`.

**Setting `relay_retention_days`:** stored through `get_setting`/`set_setting`, default 7. A value
`<= 0` disables expiry.

## Subscribing

The admin enters a URL.

**Style detection:**
- A URL whose path ends in `/inbox` is **Mastodon style**. `inbox_url = url`.
  - The relay actor is found by fetching the URL with `/inbox` replaced by `/actor`. This is the
    pub-relay and activity-relay convention. Its id and public key are stored when the fetch returns
    an actor document.
  - If the fetch fails, `actor_id` and `public_key` stay null. Deliveries then cannot be attributed to
    the relay (see "Recognising a relay delivery") until a retry fetches them.
- Any other URL is fetched as an actor document and is **LitePub style**. That includes FediBuzz's
  `https://relay.fedi.buzz/tag/<tag>` and `/instance/<host>`.
  - The style is `litepub`, `inbox_url = actor.inbox`, `actor_id = actor.id`, and `public_key` is the
    actor's key.
  - A fetch that fails or returns no `inbox` is an admin-facing error, and no row is created.

**Follow:** `follow_activity()` from the instance actor, signed as today.
- The `object` is `https://www.w3.org/ns/activitystreams#Public` for Mastodon style, and `actor_id` for
  LitePub style.
- LitePub style: `to` is `[actor_id]`. Mastodon style: no `to` or `cc`, as Mastodon's own relay Follow.
  Activity-Relay (yukimochi) routes on addressing: a Follow whose `to` holds Public is taken for an
  activity to relay and answered 202 with nothing done, so the relay never Accepts.
- The row is stored `pending` with `follow_activity_id`.
- An HTTP refusal while sending (any non-2xx) sets `failed` and `last_error`.

**Answers:** `instance_actor_answer` also looks for a relay row when no discovery-sync row matches. A
relay row matches when both hold:
- the signer's host equals the host of `inbox_url`;
- the embedded Follow (an object, or a string id) has `id == follow_activity_id`. A relay that sends
  an Accept with no embedded Follow id is matched by host alone, but only while that host has exactly
  one `pending` relay row.

Accept sets `accepted`, Reject sets `refused`, and both set `answered_at`. An Accept for a row that is
not `pending` is ignored.

**Follow-back:** a signed Follow from a relay actor whose `object` is the instance actor is answered with
an Accept (sent to `inbox_url` from the background queue) whatever the row's state. barkshark
ActivityRelay sends one to non-Mastodon software only after accepting our Follow, and its Accept does not
always arrive, so a follow-back also sets a `pending` row `accepted` and sets `answered_at`. A Follow of
anything else from a relay actor is left to the normal inbox.

**Relay actor type:** a LitePub relay's actor must have type `Application` or `Service`; otherwise adding
it fails with "is not a relay actor". A Mastodon-style `/actor` document of any other type is treated as
unknown (`actor_id` and `public_key` stay null).

**Retry:** for a `failed` or `refused` row, the actor and key are re-fetched and a new Follow is sent
with a new id. A value the re-fetch cannot find never replaces a stored actor or key. The row goes back to `pending`.
Before following again, Retry reads the relay actor's `followers` collection (and its first page), on
the actor's own host only. If it names the instance actor, the relay already accepted us and its Accept
was lost (seen with barkshark ActivityRelay 0.3.5): the row is marked `accepted` and no Follow is sent.

**Remove:** sends `Undo` of the stored Follow, wrapping the original Follow activity, and then deletes
the row. Delivery failure of the Undo does not block deletion.

## Recognising a relay delivery

Only rows in state `accepted` that have a `public_key` take part. Every other delivery is processed
exactly as today.

**Mastodon style.** The body's `actor` is the post's author. When all of the following hold, the
delivery is **relayed** and the inbox passes `relay_id` on to processing:
- the request's `Signature` header `keyId` (without its fragment) equals `actor_id`
- the HTTP signature verifies against that relay's `public_key`
- the body passes the existing checks, i.e. the author's LD signature already verified through the
  fallback

If the relay's HTTP signature does not verify, the delivery is processed as today, with no
`relay_id`.

**LitePub style.** The body's `actor` equals a relay row's `actor_id`. The gate runs after
`HttpSignature.precheck` (so the Digest and date window are already checked) and before
`find_actor_or_create_cached`, so no `User` row is created for a relay.
- **Accept and Reject** are answered by the gate itself. They are honored only for a `pending` row whose
  follow id matches the Accept's object, or when the object carries no follow id; otherwise they are
  logged and ignored. Either way the response is 200.
- **Announce** is handled only from an `accepted` row. If the object is a URI, or an object with an
  `id`, the task `process_relayed_announce(relay_id, object_uri)` is queued on the **background** Celery
  queue, and the inbox answers 200.
- **Every other activity type** from a relay actor, and an Announce from a row that isn't `accepted`,
  passes to the normal inbox path unchanged.
- The HTTP signature must verify against the relay's `public_key`. If it doesn't, the key is re-fetched
  and verification retried, but only when the signature's `keyId` (without its fragment) names the relay
  actor, at most once per relay per 300 seconds, and a fetched key replaces the stored one only if it is
  non-empty and different. If verification still fails, the response is 401 and the failure is logged.

**Pending, refused, failed or unknown relays.** Deliveries from these get no special handling. A
LitePub Announce from an actor no local user follows is dropped, as it is today.

## What is kept

The rules below apply only to relayed deliveries. Non-relayed traffic is untouched.

- **Create of a top-level object** (no `inReplyTo`):
  - with no community: routed to `/c/microblogs` by the existing `process_new_content` path;
  - addressed to a community that already exists here: routed there;
  - addressed to a community that doesn't exist here: dropped. Relays must never trigger community
    discovery or creation.
- **Create of a reply:** kept only if its parent post or reply already exists here and the thread is
  open: the post and parent are not deleted, and the community is not private, `local_only` or banned.
  The normal reply gates (locked, archived, bans, blocks) also apply. Otherwise it is dropped, and no
  parent fetch happens. A reply is stored in its parent's community and carries no `relay_id`.
- **Announce (a relayed boost)** inside a Mastodon-style relay delivery: dropped.
- **Update and Delete:** handled by the normal handlers. A relayed Update is processed only for an object
  already stored here (otherwise it is dropped, since an Update of an unknown object would create it); a
  Delete never creates anything.
- **Like, Follow and others:** processed as today.
- **`process_relayed_announce`:** fetches the object with `remote_object_to_json`. It then applies the
  same existing-community and reply-parent rules, and stores the post through
  `create_resolved_object(...)`, the path `process_microblog_announce` uses. It skips only the
  "announcer is followed" gate, records no boost, and sets `relay_id`. It passes no announce id (which
  would become `post.ap_announce_id` and be re-emitted in outboxes), skips an object whose canonical `id`
  is already stored (a relay may announce a URL that differs from it), and logs an object the creation
  path refused.
- **Moderation:** all existing gates apply unchanged. That covers inbox allowlist and banned
  instances, `ALLOWLIST_INTENSE`, `can_create_post`, the `create_post` refusals (`direct`,
  `local_only`, non-http ids), private and `local_only` communities, user and community bans, and the
  new-account limit.
- **Duplicates:** a post or reply that already exists is not modified, and its `relay_id` is not set.
- **Marking:** a post newly created from a relayed delivery gets `relay_id`.
- **No re-broadcast:** relayed content (a post or a reply created while a relay id is set) is never
  Announced to the followers of the local community it lands in.
- **Communities:** an audience naming a community that is deleted (`ap_deleted_at` set) is treated as
  unknown, so the object is dropped.

## Expiry

The Celery beat task `expire_relayed_posts` runs daily. When `relay_retention_days > 0`, it selects
posts with `relay_id IS NOT NULL` and `created_at < now - relay_retention_days`. `created_at` is when the
post arrived here, not the author's date, so a post that arrives late with an old date is not expired
the moment it lands. The daily maintenance (both the synchronous and the Celery variant) runs it.

A post is kept when any of these holds:
- a local user voted on it, replied to it, bookmarked it, or reported it;
- its author is followed by a local user (`UserFollower` with a local follower);
- it is in a community other than the `microblogs` communities that has a local member;
- it is sticky, or under review (`status <= POST_STATUS_REVIEWING`).

Every other selected post is purged through the existing post purge path, files and media included.

The task works in batches of 500, each batch in its own transaction, until no deletable post remains or
a time budget (`RELAY_EXPIRY_SECONDS`, 600, measured on a monotonic clock) is spent. A post that fails
in a run is not attempted again in that run. The CDN URLs of every deleted post are collected in one
list and flushed once at the end of the run. It logs how many posts it deleted, kept and failed.

## Admin and CLI

**Page.** `/admin/federation/relays` (permission `change instance settings`), linked from the
federation admin page.
- It lists each relay's `url`, `style`, `state`, `answered_at`, `last_error`, and the number of posts
  with that `relay_id` that arrived in the last 24 h (`created_at`).
- It has a form to add a URL, Retry and Remove buttons (POST, CSRF-protected), and a
  `relay_retention_days` field.

**CLI.** `flask relays add <url>`, `flask relays remove <url>`, `flask relays list` and
`flask relays retry <url>`, following `app/discovery/cli.py`.

## Live

Nothing changes. Relayed posts are stored through `Post.new`, so the post-stored hook wakes
`community:<id>` (for Live-capable communities) and `any` as for any other post.

## Error handling summary

| Failure | Behavior |
|---|---|
| Actor fetch fails at add (LitePub) | the admin sees an error; no row is created |
| Actor fetch fails at add (Mastodon) | the row is created; deliveries are not attributed until a retry |
| Follow delivery refused | `failed` and `last_error`; the admin can retry |
| Relay HTTP signature fails (Mastodon) | processed as non-relayed (today's behavior) |
| Relay HTTP signature fails (LitePub) | one key re-fetch, then 401 |
| Object fetch fails in `process_relayed_announce` | logged, dropped, no retry |
| Undo delivery fails on remove | the row is still deleted |

## Testing

Coverage requirements:
- 100% line and branch coverage of new and changed lines
  (`tests/check_changed_line_coverage.py --branches`);
- no drop in any floor in `coverage_floors.ini`, with a new floor of 100 for new relay modules;
- the inline-import ratchet;
- the full suite green.

Cases:

- **Style detection:**
  - inbox URL → Mastodon style;
  - actor URL → LitePub style;
  - FediBuzz tag URL → LitePub style;
  - an actor fetch failure on a LitePub URL → error and no row;
  - an actor fetch failure on a Mastodon URL → a row with a null key.
- **Follow:**
  - the object for each style;
  - the stored follow id;
  - a refusal sets `failed`.
- **Answers:**
  - Accept and Reject are matched by host and follow id;
  - wrong host → ignored;
  - wrong id → ignored;
  - an id-less Accept with exactly one pending row on that host → matched;
  - an id-less Accept with two pending rows on that host → ignored;
  - an Accept for a non-pending row → ignored.
- **Retry and remove:**
  - retry sends a new id and the row goes back to pending;
  - remove sends Undo and deletes the row, and still deletes it when the Undo fails.
- **Mastodon delivery:**
  - a valid relay signature → stored with `relay_id`;
  - a bad relay signature → stored without `relay_id`;
  - a pending relay → no `relay_id`.
- **LitePub delivery:**
  - an accepted relay → a background task is queued, the object fetch is mocked, and the post is
    stored with `relay_id`;
  - an unknown actor → today's behavior;
  - a bad signature → key re-fetch, then 401;
  - a non-Announce activity → ignored;
  - no `User` row is created for the relay.
- **Keep rules:**
  - a top-level toot → microblogs;
  - a post to an existing community → that community;
  - a post to an unknown community → dropped, and no community is created;
  - a reply whose parent is stored → kept;
  - a reply whose parent is unknown → dropped, and no fetch happens;
  - a relayed boost → dropped;
  - an existing post → unchanged.
- **Moderation through a relay:**
  - an author on a banned instance;
  - an author outside the allowlist;
  - a banned user;
  - a private or `local_only` community.
- **Expiry:**
  - an old, unengaged relayed post is purged;
  - each keep reason has its own test: vote, reply, bookmark, report, followed author, joined
    non-microblogs community, sticky, under review;
  - a non-relayed post is never touched;
  - the task loops over batches until none are left, stops at the time budget, attempts a failing post
    once per run, and flushes the CDN once;
  - `relay_retention_days = 0` disables expiry;
  - deleting a relay sets `relay_id` to null.
- **Admin and CLI:** permission is required; add, retry and remove work; the counts render; the
  retention setting saves.
- **Live:** a relayed post wakes `community:<microblogs id>` and `any`.
- **Browser and federation verification (not a committed gate):**
  1. Subscribe to a real public relay.
  2. Observe the Accept.
  3. Observe relayed posts arriving in `/c/microblogs` Live.

  A local stack at `127.0.0.1` cannot receive deliveries from internet relays. Doing this on hell.cloud
  needs the owner's go-ahead at that point.

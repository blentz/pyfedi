# Bluesky and ATProto: one feature, from copies to a native identity

Date: 2026-11-23. Extends `2026-11-13-connected-accounts-design.md` (connected accounts: CA below). Also builds on
`2026-11-21-microblog-feed-setting-design.md`, `2026-11-22-private-microblogs-design.md` and
`2026-10-19-per-domain-identity-design.md`.

## 1. Intent

Today a member can connect a Bluesky account they already own, and their posts are copied to it (CA). Nothing comes
back. This project turns that into one integrated Bluesky feature:

- **Two ways to have a Bluesky account, one feature.** A member either **creates** one here, as a native ATProto
  identity (`alice.example.org`) on the instance's own PDS, or **connects** an account they already own, by OAuth
  as today. Both are Bluesky connections on the same settings page. They use the same post-form checkboxes, the
  same copy pipeline, the same per-post status ("Posted to Bluesky ↗"), and the same retries and edit/delete sync.
- **ATProto as a federation protocol, not just a destination.** A member's Bluesky identity carries more than
  copies of their posts. It also carries replies, likes, reposts, follows and blocks. Members follow ATProto
  accounts by handle, those accounts' posts arrive in microblog feeds, and replies and likes made on Bluesky come
  back to the posts they answer.

## 2. Rulings

Owner rulings, 2026-11-23:
- **A1 Sidecar PDS.** PieFed does not implement a repo. It runs the official Bluesky PDS beside it, and the PDS owns
  the repo, signing, firehose and relay crawling.
- **A2 Per-member opt-in.** Nobody is on ATProto until they create or connect a Bluesky account.
- **A3 Per-domain handles.** A created account's handle is `<name>.<the member's home domain>`.
- **A4 Jetstream for inbound**, filtered to the DIDs that anyone here follows. Older history and missing context are
  read from the author's own PDS.
- **A5 (O1) Which posts.** Microblog posts are pre-ticked for Bluesky. Posts in other communities go as a link card,
  and only when ticked; the connection's `default_on` decides whether the box starts ticked, and for Bluesky it now
  defaults to off. This amends CA's C2/C3 and §6 for Bluesky only. Facebook, Instagram and YouTube are unchanged.
- **A6 (O2) Read-only follows.** A member without a Bluesky identity can follow ATProto accounts. That writes
  nothing anywhere. Replying to, liking or boosting ATProto content needs an identity, and the button offers to set
  one up in place (§8).
- **A7 (O3) Posts written outside PieFed.** A member's own posts made in Bluesky apps are imported as their PieFed
  microblog posts.
- **A8 (O4) One integrated feature.** A created account is a CA connection (`service = bluesky`,
  `variant = hosted`), next to the OAuth kind (`variant = oauth`). There is no second settings page, table family
  or pipeline. Each member has **at most one Bluesky identity**: the connection that their follows, likes, replies,
  notifications and imports go through. A created account is always the identity. A connected account becomes the
  identity when the member has no other identity and accepts the wider OAuth scopes (§5). Any further Bluesky
  connections only receive copies, as in CA today.
- **A9 (O5) Seamless handles.** The handle is derived without asking. `_` becomes `-`. On a collision or an invalid
  label, PieFed offers one pre-filled alternative (`alice-2`) that the member can accept with one click. The goal
  is that creating an account is a single click.
- **A10 (O6) The relay cap.** A waiting list and queuing, throttling, and selection by follower count (§9).

## 3. Platform facts that bound the design (read 2026-11-23, sources in §15)

- **Handle domains.** `PDS_SERVICE_HANDLE_DOMAINS` is comma separated, and each entry starts with `.`. The PDS
  answers `/.well-known/atproto-did` itself, matching the request Host against those suffixes. Each domain needs
  wildcard DNS and wildcard TLS routed to the PDS for that path.
- **Accounts.** `createAccount` needs an invite unless `PDS_INVITE_REQUIRED=false`. `createInviteCode` takes admin
  basic auth. Neither admin nor service auth can write into a repo, so PieFed holds every created account's own
  credentials (a random password, sealed). `admin.updateAccountPassword` recovers an account whose credentials
  were lost. `createAppPassword` needs a full session, which PieFed has.
- **Relay limits on a new PDS** (`bsky.network`): 100 accounts; 5 account creations a second; 50 events a second,
  2,600 an hour and 21,000 a day. They are raised by asking Bluesky.
- **Jetstream v2**: `wss://jetstream.us-east.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents` (and
  us-west). It filters by `dids` (at most 10,000) and `collections` (at most 100). The cursor is a `seq`, delivered
  at least once, and reconnects reach back 36 hours. **Unconfirmed:** whether a filter can change on a live socket.
  Until P0 settles it, the design reconnects with the new filter from the last cursor, which loses nothing.
- **Reads.** Repos are public: `getRecord`, `listRecords` and `sync.getBlob` on the author's PDS need no auth.
  `public.api.bsky.app` needs no auth, and its limits are unpublished ("generous").
- **OAuth scopes** are granular and enforced (CA §2). CA asks for `repo:app.bsky.feed.post?action=create&action=delete
  blob:*/*`. An identity needs more (§5).
- **Labels.** `!no-unauthenticated` means hide from logged-out viewers. The self-labels `porn`, `sexual`, `nudity`
  and `graphic-media` mark adult content. No takedown signal is documented for Jetstream consumers (unconfirmed).
- **Posts**: at most 300 graphemes, with facets at UTF-8 byte offsets, and 4 images or 1 video. Posts cannot be
  edited. `deleteRecord` deletes them.

## 4. Architecture

```
            PieFed                                                 sidecar
 ┌─────────────────────────────────────────────┐            ┌─────────────────┐
 │ app/connections/   settings page, checkboxes,│            │ Bluesky PDS      │──▶ relays
 │                    copies (SocialCrosspost), │──hosted───▶│ repos, handles,  │
 │                    dispatch, retries, locks  │  session   │ well-known       │
 │   bluesky.py       OAuth variant (CA)        │            └─────────────────┘
 │   bluesky_hosted.py hosted variant: create,  │──oauth────▶ member's own PDS (DPoP)
 │                    sessions, slots (§9)      │
 │ app/atproto/       protocol layer both use:  │
 │   resolve.py       handle → DID → PDS        │
 │   records.py       record shapes, facets     │
 │   identity.py      the identity's records    │
 │   jetstream.py     consumer process  ◀───────┼──── Jetstream (dids = followed + identities)
 │   ingest.py        records in                │
 │   notify.py        listNotifications poll    │──── AppView via the identity's PDS
 └─────────────────────────────────────────────┘
```

- **One client interface, two variants.** `app/atproto/` takes a `Session` with `xrpc(method, nsid, ...)`.
  `bluesky.py` implements it with DPoP-bound OAuth tokens, and `bluesky_hosted.py` with the PDS's own access and
  refresh JWTs (re-login with the sealed password when the refresh fails). Everything above the session (records,
  copies, identity, notifications) does not know which variant it is using.
- **Refactor, not fork.** `resolve_handle`, `did_document`, `pds_of`, `facets`, `compose`, `labels`,
  `build_record` and `upload_video` move from `app/connections/bluesky.py` into `app/atproto/`. CA's Bluesky becomes
  one user of them, and its tests move with them.
- **Dispatch seam.** ATProto actors are remote `User`s on one pseudo instance (`software = 'atproto'`, domain
  `atproto`). Outbound paths that already branch on `instance.software` (follows, votes, replies, boosts, blocks)
  gain an `atproto` arm that writes through the acting member's identity. Blocking the `atproto` instance blocks the
  whole network, using the existing tools.
- **The consumer** is `flask atproto jetstream`, one process per deployment, run as its own service. It hands
  records to Celery and never blocks on the database.

## 5. Data (amends CA §4)

- `social_connection` gains:
  - `variant` `hosted | oauth` for Bluesky. Existing Bluesky rows are migrated to `oauth`.
  - `is_identity` (bool). A partial unique index on (user_id) WHERE is_identity enforces one identity per member.
  - `import_own` (bool; A7): import posts written outside PieFed. On by default for hosted, offered and off by
    default for oauth.
  - `slot_state` for hosted rows: `active | waiting | deactivated | moved`. The waiting list itself is
    hosted rows in state `waiting`, with no remote account yet (§9).

  For hosted rows, `remote_id` is the DID, `remote_name` the handle, and `sealed_tokens` holds `{password, access,
  refresh, pds_url}`.
- **OAuth identity scopes.** An oauth connection becomes the identity by re-consenting to CA's scope plus:
  - `repo:app.bsky.feed.like`, `repo:app.bsky.feed.repost`, `repo:app.bsky.graph.follow` and
    `repo:app.bsky.graph.block`, each with `action=create&action=delete`;
  - `rpc:app.bsky.notification.listNotifications?aud=*`.
  
  It never asks for `app.bsky.actor.profile`: a connected account's profile is the member's own business.
- `social_crosspost` keeps carrying **post copies** for every service and both variants, with unchanged states.
- **`atproto_record`** is new. It holds every ATProto record that is not a post copy, both directions: replies,
  likes, reposts, follows, blocks, the profile, and inbound posts and replies. Columns: uri (unique), cid, did,
  collection, rkey, direction (`out|in`), connection_id (out), post_id, post_reply_id, user_id (the followed or
  blocked user), vote_kind, created_at. A write whose row exists is not sent again (CA C13 Q1). An inbound record
  whose uri is a row of ours (or a crosspost's `remote_id`) is our own echo, and is skipped (A7 loop guard).
- Remote ATProto actors are `User`s with `ap_profile_id = at://<did>`, `ap_id = <handle>`, the `atproto` instance,
  and `ap_inbox_url` NULL. Inbound posts and replies use their at-uri as `ap_id`.
- `atproto_cursor` is one row holding the Jetstream `seq`, written at most every few seconds.

## 6. Outbound

**Copies (posts).** These follow the CA pipeline and eligibility (`eligibility.py`): public, published, not
`local_only`, not a private community, and the private microblogs of spec 2026-11-22 stay private. For Bluesky, with
either variant:

| Post | Bluesky shape (amends CA §6 for Bluesky) | Form checkbox |
|---|---|---|
| Microblog post (local microblogs community) | Full post: text from the body. Over 300 graphemes it is cut with "…" and a link back. Images (≤4, alt text) or 1 video. Facets for links, ATProto mentions and hashtags. `langs`. NSFW/NSFL → self-labels. A CW post becomes "CW: …" plus a link, without media. | pre-ticked |
| Post in another community | The title, plus an external card linking back (title, description, thumbnail from the post's image) | `default_on`, off by default |

Edits do not reach Bluesky, and the author is told so. Delete, mod removal and purge → `deleteRecord`. A restore
sends the post again (CA §7).

**Identity records** (only through the member's identity connection):

| PieFed action on ATProto content | Record |
|---|---|
| Reply to an ATProto post or reply | `app.bsky.feed.post` with `reply {root, parent}`; deleted with the reply |
| Upvote | `app.bsky.feed.like`; un-vote deletes it; downvotes send nothing |
| Boost | `app.bsky.feed.repost`; un-boost deletes it |
| Quote | post with `app.bsky.embed.record` |
| Follow / unfollow | `app.bsky.graph.follow`, accepted at once / deleted |
| Block / unblock | `app.bsky.graph.block` / deleted |
| Profile change (hosted only) | `app.bsky.actor.profile` putRecord: name, bio plus a profile link, avatar, banner |

When a member replies to an ATProto thread, that reply is not also sent as a copy: it is the reply.

**Sessions and locks.** All writes are Celery tasks that use CA's per-connection refresh lock and its per-service
concurrency cap. Hosted writes also pass the PDS-wide budget (§9).

**Backfill when a hosted account is created.** The member's last 50 eligible microblog posts are copied with their
original `createdAt`, at low priority within the budget.

## 7. Inbound

**Following.** The follow box and search take `@handle`, `handle` or `did:`. The handle resolves to a DID, the DID
to a PDS, and `app.bsky.actor.profile/self` is read from that PDS. That gives a remote `User`. The follow adds the
DID to the consumer filter and backfills that actor's last 20 posts with `listRecords`. Read-only follows (A6) are
recorded locally only.

**The consumer** subscribes to the collections `app.bsky.feed.post`, `feed.repost`, `feed.like`, `actor.profile`
and `graph.follow`, plus `identity` and `account` events. Its DIDs are every ATProto actor with a local follower,
plus every identity's DID (for A7 imports and for follows of our members). When that set changes, it reconnects
with the new filter, at most once a minute. Above 10,000 DIDs it opens another socket.

| ATProto | PieFed |
|---|---|
| post, no reply | A post in the local microblogs community, by the remote `User` (the ActivityPub `Note` path, `find_microblogging_community`). Blobs are fetched from the author's PDS and stored like remote media. Facets become links and mentions. |
| post with `reply` | A `PostReply` under its parent. An unknown parent is fetched (`getRecord`) toward the root, at most 20 hops. |
| post embedding a record | A quote post (spec 2026-10-12 c2) |
| repost | A `PostBoost` (spec 2026-08-23) |
| like of one of our records | An upvote. Other likes are dropped. |
| follow of one of our identities | A follower row. Bluesky follows need no approval, but the member sees the follower. |
| profile | Profile refresh |
| delete | Delete here |
| `identity` | Handle updated |
| `account` inactive or taken down | Their content hidden. Deleted when the account is deleted. |
| a post by one of our identities that we did not write (A7, `import_own`) | The member's own microblog post, marked "from Bluesky". It is not copied back to Bluesky. Mastodon and others get it through the usual ActivityPub federation. |

**Notifications.** Replies, mentions, quotes, likes and reposts by people nobody here follows reach us through a
Celery beat poll of each identity's `app.bsky.notification.listNotifications` every 2 minutes, using its PDS's
AppView proxy. A copy-only connection is polled too, so that replies on the member's copies come back to the PieFed
post (via `social_crosspost.remote_id`). Polling it needs the `rpc:` scope, so such a connection's reconnect
prompt offers that scope.

**Labels and visibility.** Self-labels `porn`, `sexual` and `nudity` make a post NSFW, and `graphic-media` makes
it NSFL. `!no-unauthenticated`, on the account or the post, sets a new `Post.requires_login`: hidden from logged-out
viewers. Remote content blocklists and blocked phrases apply as usual.

**Display.** An ATProto author shows as `@handle` with a Bluesky badge, and their profile links to `bsky.app`.

## 8. Member experience

- **`/user/settings/connections`, Bluesky card.** It shows the member's Bluesky connections and two buttons:
  **Create @alice.example.org** (one click, A9) and **Connect an existing account** (CA's OAuth flow). It also has
  two switches: "Use for follows and replies" (on for hosted, offered for oauth with the wider consent) and "Bring in
  posts I write on Bluesky" (`import_own`).
- **Inline opt-in.** When a member without an identity hits Reply, Like or Boost on ATProto content, a sheet
  offers both options, with Create first. After it finishes, the action completes.
- **Status on posts.** As in CA: "Posted to Bluesky ↗", failures, Retry.
- **Off and on.** Disconnecting a hosted account deactivates it (`deactivateAccount`), so it reads as deactivated
  on Bluesky. Reconnecting reactivates it and keeps the same DID and handle. Deleting the PieFed account deletes the
  PDS account. Renaming calls `updateHandle`.
- **Leaving with it.** "Get an app password" mints one for using Bluesky apps directly; posts made there come back
  through A7. Account migration to another PDS is standard ATProto. Afterwards the connection is marked `moved`,
  and PieFed keeps it as an OAuth identity if the member reconnects.

## 9. The relay cap: slots, waiting list, throttling (A10)

- **Slots.** `ATPROTO_MAX_ACCOUNTS` (default 100) caps hosted connections in state `active`. Raising the relay
  limit is then just a change to that setting.
- **Waiting list.** When the slots are full, Create puts the member on the waiting list (`waiting`). The Bluesky
  card shows their place and offers Connect an existing account, which takes no slot, in the meantime. Read-only
  follows work while waiting.
- **Selection by follower count.** The waiting list is ordered by follower count (local plus remote followers),
  then by date joined. A daily task fills free slots from the top, and the member is notified. `ATPROTO_SLOT_POLICY`
  chooses what happens to members who already have a slot:
  - `sticky` (default): a slot is kept, but is freed after 90 days with no eligible post.
  - `ranked`: slots are recomputed daily as the top N by follower count among everyone who asked. A member who
    drops out is deactivated, and reactivated when they rank again.

  Admins can also grant or pin a slot by hand from the connections admin page, which shows the count and the list.
- **Throttling.** A PDS-wide token bucket in Redis keeps hosted writes under the relay's event limits, with
  headroom: 40 a second, 2,000 an hour and 18,000 a day, all configurable. Writes are queued by priority:
  deletes > profile > posts and replies > follows and blocks > likes and reposts > backfill. A queued write waits
  in `social_crosspost.next_attempt_at` (copies) or a Celery countdown (records), and is never dropped. Account
  creations are spaced to at most 1 a second.

## 10. Admin and configuration

- Environment: `ATPROTO_PDS_URL`, `ATPROTO_PDS_ADMIN_PASSWORD`, `ATPROTO_JETSTREAM_URL` (default us-east v2),
  `ATPROTO_MAX_ACCOUNTS`, `ATPROTO_SLOT_POLICY`, and the budget settings. Tokens and passwords are sealed under CA's
  `CONNECTIONS_SECRETS_KEY`.
- CA's per-service admin switch for Bluesky gains a second switch, "Members can create accounts here". It is
  offered only when the PDS settings are set.
- Per domain (`DomainConfig`): "Bluesky accounts on this domain". It is offered when the domain federates, is not an
  onion, and is listed in the PDS's `describeServer.availableUserDomains`.

## 11. Testing

- **Unit.** respx stands in for the PDS, the OAuth PDS, the AppView proxy and the PLC directory. Recorded Jetstream
  frames are replayed through `ingest.py`. Every row of §6, §7 and §9 has tests, for both variants wherever a
  variant applies. CA's existing Bluesky tests pass unchanged after the refactor, apart from the amended shapes
  (A5).
- **Harness.** `interop/run.sh bluesky` grows two more PieFed handle domains on the harness PDS, a self-hosted
  Jetstream reading the PDS's `subscribeRepos`, and a stub AppView for `listNotifications`. The end-to-end test:
  1. alice creates an account, and her handle resolves on her domain;
  2. her microblog post lands in her repo and on Jetstream;
  3. carol (on the second domain) is on the waiting list once the cap, set to 1 for the test, is full;
  4. bob (a plain PDS account) replies and likes, and both land on alice's post;
  5. alice follows bob, and bob's post lands in her feed;
  6. alice's reply to bob lands in bob's thread;
  7. a post bob makes as alice through an app password (A7) is imported once and not echoed back.
- **Live check (P6)** on a public deployment, with the owner.

## 12. Phases (each merges on its own)

- **P0 spike.** Harness PDS with two handle domains and well-known through the proxy. `createAccount` with invite
  and email choices (does the PDS send mail?). Self-hosted Jetstream on a PDS without a relay, and whether a filter
  can change live. `listNotifications` through the proxy. Session refresh and re-login. Whether the OAuth `rpc:`
  scope's `aud` form works on the harness PDS.
- **P1 integration groundwork.** Move the shared code into `app/atproto/`, the `Session` interface, the migration
  (variant, `is_identity`, `import_own`, `slot_state`, `atproto_record`, `atproto_cursor`, `requires_login`), and
  the A5 shape and default change.
- **P2 hosted accounts.** Create, sessions, profile, handles (A9), slots, waiting list, selection, budget,
  deactivate/reactivate/delete, rename, app passwords, admin and domain switches. Copies through the hosted variant,
  and backfill.
- **P3 inbound.** Handle lookup, remote `User`s, the consumer service, posts, reposts and replies into feeds,
  read-only follows, labels, `requires_login`.
- **P4 identity interactions** for both variants: replies, likes, reposts, quotes, follows and blocks out;
  notifications in; the inline opt-in sheet; the OAuth identity re-consent.
- **P5 lifecycle and moderation.** `identity` and `account` events, takedowns, instance block, A7 imports, moves.
- **P6 live check.**

## 13. Non-goals

- A PDS, relay or AppView inside PieFed.
- Communities as ATProto entities. Community posts go out only as their authors' link cards (A5).
- Full-network ingestion, paid Replay, feed generators, a labeler service, and `chat.bsky.*` DMs.

## 14. Deployment notes (generic)

A deployment that enables hosted accounts runs the PDS image beside PieFed. Each domain offering handles needs a
wildcard DNS record, plus wildcard or on-demand TLS, with `/.well-known/atproto-did` on `*.<domain>` routed to the
PDS. It also runs `flask atproto jetstream` as a long-lived service. `env.sample` documents the settings in §10.

## 15. Sources (read 2026-11-23)

- PDS: `github.com/bluesky-social/atproto` `packages/pds/src/config/config.ts`, `well-known.ts`,
  `api/com/atproto/server/createAccount.ts`, `createInviteCode.ts`, `admin/*`; `github.com/bluesky-social/pds` README.
- Relay limits: `docs.bsky.app/docs/advanced-guides/rate-limits`; `atproto.com/guides/going-to-production`.
- Jetstream: `bsky.network/docs/jetstream/`; `github.com/bluesky-social/jetstream-legacy`.
- Labels: the moderation guide on `docs.bsky.app`; `atproto.com/specs/label`.
- OAuth scopes: CA §2 and §13.

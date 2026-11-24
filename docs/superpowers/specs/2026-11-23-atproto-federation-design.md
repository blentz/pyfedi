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
- **A9 (O5) Seamless handles.** The handle is derived without asking. `_` becomes `-`. A member may have only their
  own name or one of its numbered alternatives (`alice`, `alice-2` … `alice-99`), never another member's or the
  site's. An alternative that is another member's own name (`bob-2` is `bob_2`'s) is skipped; two names that clean
  up alike (`ann_lee`, `ann-lee`) share the base, first come. On a collision or an invalid
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
  at least once, and reconnects reach back 36 hours. **A v2 filter cannot change on a live socket** (P0, §16): a
  client data frame closes it (1008). The consumer reconnects with the new filter from the last cursor, which loses
  nothing.
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
  one user of them (its tests import them from there).
- **Dispatch seam.** ATProto actors are remote `User`s on one instance (`software = 'atproto'`, domain
  `bsky.app`). Outbound paths that already branch on `instance.software` (follows, votes, replies, boosts, blocks)
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
- **OAuth scopes.** Every Bluesky sign-in now asks for CA's scope plus
  `rpc:app.bsky.notification.listNotifications?aud=*`, so replies to copies come back (§7). An oauth connection
  becomes the identity by signing in again ("Use for follows and replies") for that scope plus
  `repo:app.bsky.feed.like`, `repo:app.bsky.feed.repost`, `repo:app.bsky.graph.follow` and
  `repo:app.bsky.graph.block`, each with `action=create&action=delete`. A sign-in that grants less changes nothing.
  The client metadata declares the widest scope. Tokens keep the scope granted.

  It never asks for `app.bsky.actor.profile`: a connected account's profile is the member's own business.
- `social_crosspost` keeps carrying **post copies** for every service and both variants, with unchanged states.
- **`atproto_record`** is new. It holds every ATProto record that is not a post copy, both directions: replies,
  likes, reposts, follows, blocks, the profile, and inbound posts and replies. Columns: uri (unique), cid, did,
  collection, rkey, direction (`out|in`), connection_id (out), post_id, post_reply_id, user_id (the followed or
  blocked user), vote_kind, created_at. A write whose row exists is not sent again (CA C13 Q1). An inbound record
  whose uri is a row of ours (or a crosspost's `remote_id`) is our own echo, and is skipped (A7 loop guard).
- Remote ATProto actors are `User`s with `ap_profile_id = ap_public_url = https://bsky.app/profile/<did>`, `ap_id =
  user_name = <handle>`, the `bsky.app` instance, and `ap_inbox_url` NULL. Inbound posts and replies use their bsky.app
  address (`https://bsky.app/profile/<did>/post/<rkey>`) as `ap_id`, since PieFed keeps only http(s) ids (D1406);
  `atproto_record` keeps the at:// URI. `user.requires_login` carries an account's `!no-unauthenticated`.
- `atproto_cursor` holds the Jetstream `seq`, written at most every few seconds: one row per Jetstream URL (its id
  is the URL's CRC-32), since each Jetstream numbers its own events.

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
| Quote: a microblog post whose text links a Bluesky post (PieFed has no quote button; a link is how Bluesky's apps start one) | its copy embeds `app.bsky.embed.record`, or `recordWithMedia` with its pictures or video (P4) |
| Follow / unfollow | `app.bsky.graph.follow`, accepted at once / deleted |
| Block / unblock | `app.bsky.graph.block` / deleted |
| Profile change (hosted only) | `app.bsky.actor.profile` putRecord: name, bio plus a profile link, avatar, banner |

When a member replies to an ATProto thread, that reply is not also sent as a copy: it is the reply. A thread is on
Bluesky when its post and the reply answered both have records (one that came in, a member's reply their identity
sent, or a post's copy) and one of the two is an ATProto account's. A reply that stays here (a non-federating
domain's member or community, or followers-only) is not sent.

**Reconciled, not replayed (P4).** Each identity record is made to match what PieFed holds now (the vote, boost,
reply, follow or block): `app.atproto.identity.sync`, a Celery task per change, one at a time per record (a Redis
lock). Its `atproto_record` row, with its record key, is written before the record is made, so a write whose answer
was lost is found on the PDS rather than made twice, and the record's Jetstream echo is known as ours. Private votes
send nothing. A member with no identity sends nothing; when one appears, every follow and block of an ATProto
account and the last 7 days of votes, boosts and replies are reconciled, so what they did first goes out then.
`flask send-queue` takes up rows a lost task left half made.

**Sessions and locks.** All writes are Celery tasks that use CA's per-connection refresh lock and its per-service
concurrency cap. Hosted writes also pass the PDS-wide budget (§9).

**Backfill when a hosted account is created.** The member's last 50 eligible microblog posts are copied with their
original `createdAt`, at low priority within the budget.

## 7. Inbound

**Following.** The follow box and search take `@handle`, `handle` or `did:`. The handle resolves to a DID, the DID
to a PDS, and `app.bsky.actor.profile/self` is read from that PDS. That gives a remote `User`. The follow adds the
DID to the consumer filter and backfills that actor's last 20 posts with `listRecords`. Read-only follows (A6) are
recorded locally only.

**The consumer** (`flask atproto-jetstream`, `entrypoint_jetstream.sh`) subscribes to the collections `app.bsky.feed.post`, `feed.repost`, `feed.like`, `actor.profile`
and `graph.follow`, plus `identity` and `account` events. Its DIDs are every ATProto actor with a local follower,
plus every identity's DID (for A7 imports and for follows of our members). When that set changes, it reconnects
with the new filter, at most once a minute (`ATPROTO_JETSTREAM_RECHECK`), and reads the latest posts of any actor that
joined it (what it wrote between the follow and its socket opening, when there is no cursor to replay it). Each event
goes to a Celery task (`app.atproto.ingest.event_task`), which both worker entry points import. Each socket carries at most 5,000 DIDs, and more DIDs mean more sockets.
The filter travels in the URL: Jetstream's cap is 10,000 query parameters, DIDs and collections together, and the
harness took 9,000 DIDs (a 342 KB URL). 5,000 leaves headroom for proxies in front of a public endpoint (§16).

| ATProto | PieFed |
|---|---|
| post, no reply | A post in the local microblogs community, by the remote `User` (the ActivityPub `Note` path, `find_microblogging_community`). Blobs are fetched from the author's PDS and stored like remote media. Facets become links and mentions. |
| post with `reply` | A `PostReply` under its parent. An unknown parent is fetched (`getRecord`) toward the root, at most 20 hops. |
| post embedding a record | A link to the quoted post after the text (P3); a quote post (spec 2026-10-12 c2) in P4 |
| repost | A `PostBoost` (spec 2026-08-23) |
| like of one of our records | An upvote. Other likes are dropped. |
| follow of one of our identities | A follower row. Bluesky follows need no approval, but the member sees the follower. |
| profile | Profile refresh |
| delete | Delete here |
| `identity` | Handle updated |
| `account` inactive or taken down | Their content hidden. Deleted when the account is deleted. |
| a post by one of our identities that we did not write (A7, `import_own`) | The member's own microblog post, marked "from Bluesky". It is not copied back to Bluesky. Mastodon and others get it through the usual ActivityPub federation. |

**Notifications.** Replies, mentions, quotes, likes, reposts and follows by people nobody here follows reach us
through a poll of each Bluesky connection's `app.bsky.notification.listNotifications` every 2 minutes (queued by
`flask send-queue`, `app.atproto.notify`), using its PDS's AppView proxy. Each notification's record goes through the
same code as a Jetstream event. A copy-only connection is polled too, so that replies on the member's copies come
back to the PieFed post (via `social_crosspost.remote_id`). Polling it needs the `rpc:` scope, which new sign-ins
ask for; one connected before that is not polled until it signs in again.

**Answers to our records (P4).** A reply to a copy or to a reply an identity sent lands under it, a like of one is an
upvote, a repost of a copy is a boost, and a follow of an identity is a follower with a notification. A mention of a
member's Bluesky account is a Mention of the member (its link stays the bsky.app profile, which never becomes a
microblog post's link). A quote post names what it quotes as its Note's `quote` (spec 2026-10-12 C2): a member's post
when it quotes a copy. A member's own Bluesky accounts are never ATProto actors here: what they write outside PieFed
is A7's import (P5).

**Labels and visibility.** Self-labels `porn`, `sexual` and `nudity` make a post NSFW, and `graphic-media` makes
it NSFL. `!no-unauthenticated` on a post or reply sets its `requires_login`, and on the account the
user's. Either stamps the content `local_only`, which every list, page and API read already keeps from logged-out
readers (a Bluesky record is never federated from here anyway); so is a reply under such a post. When an account
changes its flag, its content and the ATProto replies under its posts are stamped again. The account's profile is
hidden from logged-out readers too.

A handle is taken only when it resolves back to the DID that claims it (ATProto's two-way check); otherwise the
actor is named by its DID, and a handle another actor held moves to the one it now resolves to. Remote content blocklists and blocked phrases apply as usual.

**Display.** An ATProto author shows as `@handle` with a Bluesky badge, and their profile links to `bsky.app`.

## 8. Member experience

- **`/user/settings/connections`, Bluesky card.** It shows the member's Bluesky connections and two buttons:
  **Create @alice.example.org** (one click, A9) and **Connect an existing account** (CA's OAuth flow). It also has
  two switches: "Use for follows and replies" (on for hosted, offered for oauth with the wider consent) and "Bring in
  posts I write on Bluesky" (`import_own`).
- **Inline opt-in.** A member without an identity sees, on an ATProto post's page, an offer of both options,
  with Create first, and that what they do there reaches Bluesky once one is ready. Create and Connect come back to
  the post. Their likes, boosts and replies are recorded here at once, as for anyone; the identity's catch-up (§6)
  sends them when it starts, so the action completes. (P4 shows the offer on the page rather than intercepting each
  button: the actions never wait on a sign-in, and nothing is lost if the member leaves halfway.)
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
  - `sticky` (default): a slot is kept, but is freed after 90 days with no eligible post: the account is turned off
    (deactivated) and the member told; turning it on again joins the waiting list if no slot is free.
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
- Per domain: "Bluesky accounts on this domain" (on, off, or as the instance), on the domain's Connected accounts
  page beside CA's per-domain service switches (C12). It is offered when the domain federates, is not an onion, and
  is listed in the PDS's `describeServer.availableUserDomains`.
- `ATPROTO_HOSTED_LIMIT` (default 100) caps the accounts the PDS holds at all, turned off ones included: the relay
  counts every account a PDS has, so a new account waits once it is reached, while a turned-off one may still come
  back.

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

- **P0 spike (done 2026-11-23, §16).** Harness PDS with two handle domains and well-known through the proxy.
  `createAccount` with invite and email choices. Self-hosted Jetstream on a PDS without a relay, and whether a filter
  can change live. `listNotifications` through the proxy. Session refresh and re-login. Whether the OAuth `rpc:`
  scope's `aud` form works on the harness PDS.
- **P1 integration groundwork.** Move the shared code into `app/atproto/`, the `Session` interface, the migration
  (variant, `is_identity`, `import_own`, `slot_state`, `atproto_record`, `atproto_cursor`, `requires_login`), and
  the A5 shape and default change.
- **P2 hosted accounts.** Create, sessions, profile, handles (A9), slots, waiting list, selection, budget,
  deactivate/reactivate/delete, rename, app passwords, admin and domain switches. Copies through the hosted variant,
  and backfill.
- **P3 inbound (done 2026-11-24).** Handle lookup (`/u/<handle>`, `/u/<did>`), remote `User`s, the consumer service,
  posts, reposts, replies and deletes into feeds, profile and handle updates, read-only follows with a backfill of
  20, labels, `requires_login`. Likes, follows of our identities, quotes as quote posts and notifications are P4;
  `account` events and A7 imports P5.
- **P4 identity interactions (done 2026-11-24)** for both variants: replies, likes, reposts, quotes, follows and
  blocks out; likes, follows, replies, reposts and quotes of our records in; notifications in; the inline offer; the
  OAuth identity re-consent; the Bluesky badge.
- **P5 lifecycle and moderation.** `identity` and `account` events, takedowns, instance block, A7 imports, moves.
- **P6 live check.**

## 13. Non-goals

- A PDS, relay or AppView inside PieFed.
- Communities as ATProto entities. Community posts go out only as their authors' link cards (A5).
- Full-network ingestion, paid Replay, feed generators, a labeler service, and `chat.bsky.*` DMs.

## 14. Deployment notes (generic)

A deployment that enables hosted accounts runs the PDS image beside PieFed. Each domain offering handles needs a
wildcard DNS record, plus wildcard or on-demand TLS, with `/.well-known/atproto-did` on `*.<domain>` routed to the
PDS. It also runs `flask atproto-jetstream` (`entrypoint_jetstream.sh`) as a long-lived service, under an init that
passes SIGTERM on (`tini`). `env.sample` documents the settings in §10.

## 15. Sources (read 2026-11-23)

- PDS: `github.com/bluesky-social/atproto` `packages/pds/src/config/config.ts`, `well-known.ts`,
  `api/com/atproto/server/createAccount.ts`, `createInviteCode.ts`, `admin/*`; `github.com/bluesky-social/pds` README.
- Relay limits: `docs.bsky.app/docs/advanced-guides/rate-limits`; `atproto.com/guides/going-to-production`.
- Jetstream: `bsky.network/docs/jetstream/`; `github.com/bluesky-social/jetstream-legacy`.
- Labels: the moderation guide on `docs.bsky.app`; `atproto.com/specs/label`.
- OAuth scopes: CA §2 and §13.
- Jetstream v0.4.4 source: `internal/subscribe/doc.go` (v2 is server-push only), `internal/ingest/orchestrator/config.go`.

## 16. P0 spike (2026-11-23)

There are two runs, both on the harness PDS (0.4.5051), its PLC directory, and Jetstream v0.4.4:
- `interop/run.sh atproto-spike` runs `interop/spike_atproto.py`, which plays the hosted variant with the PDS admin
  password and also serves a stand-in AppView at `appview.test`. 34/34 steps passed.
- `interop/run.sh bluesky-spike --identity` runs CA's OAuth spike with §5's identity scope. 29/29 steps passed.

The report is `interop/spike_atproto/report.json`.

**Hosted accounts**
- **Handles.** `PDS_SERVICE_HANDLE_DOMAINS=.pds.test,.piefed.test,.tube.test` makes `describeServer` list all
  three. `alice.piefed.test` and `carol.tube.test` resolve by `/.well-known/atproto-did` through the reverse proxy
  (`*.piefed.test, *.tube.test → pds`). The DID document names the handle and the PDS.
- **Invites.** With `PDS_INVITE_REQUIRED=true`, `createAccount` refuses without a code (`InvalidInviteCode`). A code
  minted with admin auth and `useCount: 1` works once and is then "not available".
- **Email.** A made-up address (`atproto+<id>@<domain>`) is accepted, and the account reads
  `emailConfirmed: false`. The harness PDS has no SMTP, so whether a PDS with SMTP mails it was not observed.
  Deployments should leave the PDS's email settings unset: PieFed holds the credentials and never needs a reset mail.
  PieFed gives each account a random address under `accounts.invalid` (RFC 2606, never delivered; the PDS accepts
  it), so even a PDS given SMTP mails a reset to nobody.
- **Deleting.** A deletion the PDS has not confirmed keeps its row (`deleting`: no copies, no place, still the
  password) and is asked again daily, so no live account is left that PieFed can no longer reach.
- **Session lifetimes.** Access tokens last 2 hours and refresh tokens 90 days. `createSession` with the sealed
  password works, by DID, after any loss.
- **Refresh replay.** Unlike OAuth, a password session's **used refresh token is accepted again within a grace
  period**, and the rotated session survives. A lost refresh answer therefore does not disconnect a hosted account.
  The per-connection lock stays, because OAuth needs it.
- **App passwords.** `createAppPassword` works from PieFed's session, and the app password signs in by handle (§8).
  `putRecord` on the profile works.
- **AppView proxy.** With no header, the PDS forwards `app.bsky.*` to `PDS_BSKY_APP_VIEW_URL` with a service-auth
  JWT (iss = the member's DID, aud = the AppView DID, `lxm` = the method, 60 s lifetime). An explicit `atproto-proxy`
  header works too.
- **Lifecycle.**
  - `deactivateAccount` (JSON body) leaves `getRecord` answering 200 with an empty `{}`. The ingest code reads a
    record with no `value` as unavailable.
  - `activateAccount` must be sent **without** a body, or it is refused with 400.
  - `updateHandle` keeps the DID; the new handle resolves and the old one returns 404 at once.
  - `admin.updateAccountPassword` recovers a lost password, and `admin.deleteAccount` removes the repo.

**Jetstream**
- **Bootstrap needs a relay.** Self-hosted, v2 bootstrap walks the relay's `com.atproto.sync.listHosts`, which a PDS
  lacks, so it stays in bootstrap (503). In the harness, `JETSTREAM_BACKFILL_REPOS` (a debug setting) set to one
  DID that never exists ends bootstrap at once. The live tail then follows the PDS's `subscribeRepos` and verifies
  commits against the harness PLC. This affects only the harness: production uses Bluesky's public instances.
- **Filtering and resuming.** A `dids` + `collections` filter streams only that account's posts, as
  `network.bsky.jetstream.subscribeEvents#commit` payloads carrying `did`, `collection`, `rkey`, `operation`,
  `record`, `cid`, `rev` and `seq`. A reconnect with `cursor=<seq>` replays that event (inclusive).
- **No live filter change.** A client frame (v1's `options_update`) closes the socket with 1008, "unexpected data
  message".
- **Filter size.** 10,001 DIDs are refused before the upgrade by the query-parameter cap ("number of URL query
  parameters exceeded limit"). 2,000, 5,000 and 9,000 DIDs (76 KB to 342 KB of URL) were accepted through Caddy.
  Hence the 5,000-per-socket shard in §7.

**OAuth identity scope.** §5's scope is granted in full. Creating `app.bsky.feed.like` and `app.bsky.graph.follow`
records works. `listNotifications` through the PDS proxy works with `rpc:...?aud=*`. CA's findings still hold: a
replayed OAuth refresh token kills the session, and `putRecord` without `action=update` is refused.

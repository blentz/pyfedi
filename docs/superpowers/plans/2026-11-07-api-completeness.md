# API completeness: as much of the fediverse's client apps as PieFed can serve

Date: 2026-11-07
Status: decisions D1-D5 ruled 2026-11-07 (§6); next is W0.1, the shared read layer. Nothing is built.

Owner goal (2026-11-07): "support as much of the fediverse as we possibly can". This answers M1 of
`docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md`: every client API family worth its cost, not
Mastodon's alone.

## 1. Where we start

The client-app harness (`interop/apps/`, 21 apps, two clean cold runs) measured it:

- PieFed's own `/api/alpha` already serves the Lemmy-family apps that matter: Voyager (web, Android), Thunder, Summit,
  Blorp, Photon, and by source Mlem (iOS) and Interstellar. All pass login, view and post.
- Podcast players work through the Podcasting 2.0 RSS feeds (AntennaPod driven; the rest by the feed).
- Everything else stops at its first call: Mastodon and Pixelfed apps (no OAuth app registration), PeerTube apps
  (no `/api/v1/oauth-clients/local`, `/api/v1/videos`), Jerboa (no `/api/v3/user/login`, and it refuses nodeinfo
  that does not say `lemmy`).

A survey of every other client API family (source-read, 2026-11-07) ranked what is left by reach, cost and fit:

| Family | New reach | Cost | Fit with PieFed's model | Verdict |
|---|---|---|---|---|
| Mastodon client API | Highest: every Mastodon app; Pixelfed apps (with extras); Fedilab (it sends unknown software, piefed included, to the Mastodon API); GoToSocial/Akkoma-style clients | Large (OAuth server + ~40 endpoints for a usable app) | Good for microblogs and replies; community posts need a mapping (spec M2-M5) | **Do** |
| PeerTube `/api/v1` | High: NewPipe (any instance via `/api/v1/config`, ~12 read endpoints), the official PeerTube app (any host with a local account, gated on `serverVersion` >= 7.2.0), Fedilab's PeerTube mode | Medium (15 read, ~35 with login, +5 resumable upload) | Good: video_channel communities = channels, video posts = videos | **Do** |
| Lemmy `/api/v3` (0.19) | Low-medium: Jerboa (only if it sees `lemmy` in nodeinfo), Eternity and Alexandrite (stale), closed apps | Medium: same envelopes as alpha, renamed fields (`name`/`title`, `creator_id`/`user_id`, `content`/`body`, `username_or_email`), ~30 endpoints | Very good | **Decide** (D3) |
| Pleroma/glitch emoji reactions | Moshidon, Fedilab, Tusky in part | Tiny (3-4 endpoints) | Good (PieFed has emoji reactions) | **Do, inside Mastodon** |
| gpodder.net sync | AntennaPod, Kasts, gPodder | Small (~6 endpoints) | Good (podcast communities) | **Do, last** |
| Lemmy `/api/v4` (1.0) | None until 1.0 ships (now 1.0.0-beta.3) | — | — | Watch |
| Loops app API | None: the app is wired to loops.video | — | Good | Watch |
| Misskey | Low outside Japan; Fedilab users get Mastodon instead | High (RPC, streaming, drive, MiAuth, nodeinfo spoofing) | Microblogs only | Not now |
| Mbin | None new (Interstellar speaks PieFed) | High | Good | Skip |
| Funkwhale/Subsonic, WriteFreely, Mobilizon, AP C2S | Nil (stale or no clients) | — | Low | Skip |

## 2. Shape of the work

### 2.1 Foundations (W0), before any new API

- **W0.1 A shared read layer.** `/api/alpha`'s reads mix the query (feed type, sort, paging, blocks, NSFW and
  NSFL, private-instance and domain scope, bans) with building alpha-shaped dicts (`app/api/alpha/utils/*.py`,
  `views.py`). Each new API must apply the same visibility rules, so the queries move into one module returning model
  rows, with alpha rebuilt on it first and its whole test suite as the proof nothing changed. Writes are already shared
  (`app/shared/`). This is the riskiest step and the one every later API leans on.
  **W0.1 steps** (branch `feat/shared-read-layer`, each step its own commit, full suite + floors + changed-line gates
  green after each):
  1. `app/reads/viewer.py`: a `Viewer` built once per request from the user id (the user or none, blocked people,
     communities, instances and domains, private-community memberships, read languages, the storefront). Every block
     list `get_post_list`, `get_post_list2`, `get_reply_list`, `get_post_replies` and the user listings compute today
     comes from it.
  2. `app/reads/posts.py`: one query builder for post listings, `post_listing(viewer, listing)`, taking a `Listing`
     (scope: All, Local, Popular, Subscribed, Moderating, ModeratorView, a community by id or name, a feed, a topic,
     an author; sort; the viewer's toggles: liked, saved, NSFW, minimum score, search query and type, ignore stickies)
     and returning the filtered, ordered SQLAlchemy query. `get_post_list` (with its raw-SQL fast path, whose criteria
     the same module derives) and `get_post_list2` (keyset pages) both page and view it. Where the two copies disagree
     today, the difference is listed in the commit and kept for the caller that has it, never silently unified.
  3. `get_post` and `get_post_replies`/`get_reply_list` read a post and its replies through `app/reads` (visibility
     of one post, a reply tree with the viewer's blocks).
  4. User lookup and user listings (`get_user`, `get_user_details`, `get_user_list`).

  Rules: code moves before it changes; the alpha tests are not edited (a test that must change means behaviour
  changed, which W0.1 does not); `app/reads` returns model rows and queries, never alpha-shaped dicts; no inline
  imports. New tests in `tests/test_reads_*.py` cover the layer directly, so a later API can rely on it.

  **Divergences kept in step 2, for the owner to rule on.** `get_post_list` (offset pages, raw-SQL fast path) and
  `get_post_list2` (keyset pages) disagreed when they were merged into `app/reads/posts.py`. Each is a field of
  `Spelling` there (`OFFSET_PAGES`, `KEYSET_PAGES`), so neither listing changed. Line numbers are of
  `app/api/alpha/utils/post.py` at f14b669e6.
  1. Read languages: `get_post_list` filters them only in its fast path's SQL (191-193), so a narrowed listing, a
     search or a liked/saved list shows every language; `get_post_list2` filters its query (1167-1169).
     (`languages_in_query`)
  2. Hidden posts: `get_post_list` leaves out the posts the viewer hid, except in a search (514-535);
     `get_post_list2` lists them. (`hides_hidden_posts`)
  3. Polls and events: `get_post_list2` leaves them out (1301); `get_post_list` lists them.
     (`hides_polls_and_events`)
  4. Active: both order by `last_active`; `get_post_list2` lists only posts with a reply (1292-1294, 1398)
     and `get_post_list` does not (646-650). (`active_needs_replies`)
  5. New: `get_post_list` orders any sort starting `New` as New (639); `get_post_list2` only `New` (1288).
     (`new_sorts_by_prefix`)
  6. Tiebreaks: `get_post_list2` orders New, Active and the named Top sorts a second time with `post.id` last
     (1339-1399), so its ORDER BY repeats the sort column (`score DESC, score DESC, id DESC`). Kept as it was,
     because a keyset cursor carries a value for every ORDER BY column and a changed list would refuse cursors handed
     out before. A `Top...` sort with no window of its own, and Old, get no tiebreak; `get_post_list` has none.
     (`keyset_tiebreaks`)
  7. Hide-read while searching: both skip `hide_read_posts` in a search (496, 1208), but `get_post_list2` applied it a
     second time without that condition (1321), so it hides read posts in a search too.
     (`hides_read_when_searching`)
  8. The fast path's own rules, which `get_post_list` has only when it takes that path (front page, Local, Popular,
     Subscribed, Moderating, a URL search) and `get_post_list2` never has: posts of a banned community are left out
     (`c.banned = FALSE`, 669); a Local listing honours instance blocks (187-189; neither ORM query does); and it
     reads at most 1000 posts (688), so its later pages are empty.
  Same in both and unchanged: blocks, private communities, visibility, local-only, NSFW/NSFL, bots, reposters,
  gen-AI, keyword filters, minimum score, stickies, Hot and Scaled with the domain boost. `get_post_list2` applied
  liked, saved, read-posts and keyword filters twice (1310-1337); the second copies selected nothing new and are gone.
  `get_post_list`'s raw-SQL criteria for listings that turn the fast path off (a narrowed listing, liked, saved) were
  never run and are gone.
  **Divergences kept in step 3, for the owner to rule on.** A post's replies are read three ways: `get_post_replies`
  through `app.post.util.post_replies` (the whole thread, also the post page's) or `get_comment_branch` (one branch),
  and `get_reply_list`'s conversation (a `post_id` or `parent_id` alone, a page at a time). They now share
  `app/reads/replies.py`, each with its own named spelling (`WHOLE_THREAD`, `ONE_BRANCH`; `TREE_BLOCKS`,
  `CONVERSATION_BLOCKS`, `LISTING_BLOCKS`; `_tree_order` beside `_listing_order`), so none changed. Line numbers are
  at 0bcd671cd: `util.py` is `app/post/util.py`, `reply.py` and `post.py` are under `app/api/alpha/utils/`.
  1. The post itself: `get_post_replies` refuses a post the viewer may not see, one hidden here and one nobody holds
     (`post not found`, `a_visible_post`, post.py 570, 580) and a parent nobody holds (`comment not found`, 567);
     `get_reply_list` checks the parent only (`Comment with parent_id not found.`, reply.py 199-201) and never looks
     the post up, so a post the viewer may not see lists its replies (each one a stub where `can_view` refuses it).
  2. Private communities: `get_post` refuses a non-member (`Private community - membership required`, views.py 382,
     now `app.reads.post.refuse_unreadable_post`), but neither reply read does: `can_view` knows nothing of a private
     community, so a non-member, signed in or not, reads a private community's replies through both. Probably a leak;
     unchanged here. (And `get_post` answers `membership required` to an anonymous reader where the post listings'
     gate, `refuse_private_community`, answers `authentication required`.)
     **Confirmed and FIXED 2026-11-08** (owner: fix on this branch): `app.reads.post.refuse_private_thread` refuses a
     private community's thread, branch or community comment list to non-members, with the listings' texts, and wider
     comment listings leave its comments out (`app.reads.replies._without_unjoined_private`); tests in
     `tests/test_private_community_replies.py`. The `get_post` text difference above remains.
  3. Instance blocks: the tree keeps a reply with no instance (`instance_id IS NULL`, util.py 190, 262); both of
     `get_reply_list`'s reads drop it (`NOT IN` is NULL there, reply.py 181, 313-325). (`spares_unknown_instance`)
  4. Blocked people: the tree drops the reply and its replies fall out with it when the tree is built (util.py 193-195,
     265-267); the conversation drops them in the query by their path (reply.py 298-311), as it pages rows before any
     tree. The same rows reach a tree; a conversation's pages differ. (`takes_descendants`)
  5. A viewer's settings: the tree drops bots' replies for a viewer who ignores bots or reposters (util.py 191-192,
     263-264), replies under their `reply_hide_threshold` unless they moderate the post (204-206, 276-278) and replies
     in languages they do not read (207-209, 279-281); the conversation applies none of these.
  6. Hide threshold: the whole thread spares a reply that is not collapsible (util.py 206); one branch does not (278).
     (`hide_threshold_spares_uncollapsible`)
  7. Silenced instances: the whole thread drops their replies for an anonymous reader (util.py 211-212); one branch and
     the conversation do not. (`silenced_instance_ids`, which post_replies asks)
  8. Sorts: Hot is `ranking` alone in the tree (util.py 218) and `ranking, posted_at` in the conversation (reply.py
     334); Top is `score` (220) against `up_votes - down_votes` (337-338); the tree knows hot, top, new and old and
     orders nothing for any other sort, the conversation knows the Top windows, Controversial and Relevance too and
     orders anything else (Active included) as New.
  9. How much: the whole thread reads at most 2000 replies (util.py 226); one branch and the conversation read all.
     (`limit`)
  Same in all three and unchanged: a reply hidden here goes, and its replies with it; a reply the viewer may not see is
  a stub, not a gap; `max_depth` is at most that depth (under the parent, relative to it). Not a divergence but a
  finding: both tree reads had `comments.filter(PostReply.score > -20)` for an anonymous reader with its result thrown
  away (util.py 213, 283), so no score floor ever applied; the no-op is gone, the floor is the owner's to restore or
  drop. `get_reply_list`'s flat listing keeps its own rules (blocks without descendants, `max_depth` below that depth,
  reply.py 190, against at most it in a conversation, 291).
- **W0.2 One OAuth 2 authorization server** (Authlib, already a dependency): app registration, authorization code
  with PKCE, out-of-band codes, refresh, client credentials, revocation, a settings page listing authorised apps.
  Mastodon, Pixelfed and Mbin-style clients use it; PeerTube's password grant and Lemmy's JWT login sit beside it on the
  same token table. Tokens stored hashed, scoped, revocable; a banned or deleted user's die with them.
- **W0.3 Per-host identity.** Some apps choose their protocol from nodeinfo `software.name` (Jerboa needs `lemmy`,
  Fedilab maps `peertube`/`misskey`/`pleroma`), and PieFed's own clients need `piefed`. If D3 or D4 needs another
  name, it is served per host through the multi-domain storefront machinery, never on the main domain.
- **W0.4 Harness.** Each API grows its probes into real drivers as it lands; new families get probes first so the
  matrix shows the gap. NewPipe, the PeerTube app and Fedilab join the Android tier; Tusky, Mastodon, Moshidon,
  PixelDroid and Phanpy/Elk replace their probes.

### 2.2 Workstreams

Each workstream: a spec with owner rulings, then phases, each merged on its own branch, harness-proved (its matrix
rows flip from `unsupported` to `passed`), with 100% line and branch coverage of what it adds and the full suite,
floors and changed-line gates.

- **W1 Mastodon client API** (spec 2026-11-07, rulings M2-M8 open).
  - W1.0 Endpoint inventory from the apps' source: what Tusky, Mastodon Android, Moshidon, Phanpy, Elk, Ice Cubes,
    PixelDroid and Pixelfed's app call at login, home and compose, which calls are fatal on 404 at startup, and which
    entity fields their decoders require (a missing non-null field crashes Tusky). Tusky done 2026-11-07 (source at
    43ae0bb, below); the others' inventory was stopped by a safeguard false positive and is to be rerun.

    What Tusky, the strictest decoder (Moshi codegen: a missing or null non-null field fails the whole page), requires:
    - **Login:** `POST /api/v1/apps` (form: `client_name`, `redirect_uris=oauth2redirect://com.keylesspalace.tusky/`,
      `scopes=read write follow push`); `/oauth/authorize` with `response_type=code` and a custom-scheme redirect, **no
      PKCE, no state**; `POST /oauth/token` (form, authorization_code); then `verify_credentials`, whose failure blocks
      the login. HTTPS on 443 only.
    - **Required fields.** Account: `id`, `username`, `acct`, `created_at` (RFC 3339 with a zone), `note`, `url`,
      `avatar`, `header`. Status: `id`, `account`, `content`, `created_at`, `emojis`, `reblogs_count`,
      `favourites_count`, `replies_count`, `sensitive`, `spoiler_text`, `visibility`, `media_attachments`, `mentions`.
      Attachment: `id`, `url`, `type`. Instance v2: `domain`, `version`. Relationship: nine booleans. Marker:
      `last_read_id`, `version`, `updated_at`. Filter `context` and `filter_action` are strict enums, and appear inside
      `Status.filtered`, so one wrong value fails a timeline page.
    - **Ids** are compared by length, then lexically: numeric strings that only grow (PieFed's integer ids qualify).
    - **Paging:** home uses `max_id`/`since_id`; public, tag, list, account and conversation timelines page **only**
      through the `Link: rel="next"` header; without it paging stops after the first page.
    - **Media:** `POST /api/v2/media` only (no v1); 200 = processed, 202 = processing, then `GET /api/v1/media/{id}`
      must answer **206** while processing and 200 when done (202 or 404 fails the post). `PUT /api/v1/media/{id}`
      sets description and focus.
    - **Statuses:** `POST /api/v1/statuses` and `PUT /api/v1/statuses/{id}` take **JSON** bodies with an
      `Idempotency-Key` header; edit (`/source`, `/history`) is offered with no version gate.
    - **Tolerated at startup** (logged, no crash): announcements, push subscription, markers, custom emojis, filters
      (v2, then v1 on 404 only), notification policy (404 = unsupported). `/api/v2/instance` falls back to v1 **only on
      404**; any other v2 error, a decode error included, skips v1.
    - **No software gating:** no nodeinfo read, the version string never parsed; only `api_versions.mastodon >= 5`
      unlocks the blur filter action and `configuration.translation.enabled` the translate menu.
  - W1.1 Login: OAuth (W0.2), `/api/v2/instance` and a full v1, `verify_credentials`, accounts.
  - W1.2 Reading: timelines, statuses and context, notifications, search, markers, custom emojis.
  - W1.3 Writing: statuses, media v2, favourite, boost, bookmark, follow; Pleroma/glitch emoji reactions.
  - W1.4 Pixelfed extras after a spike (`/api/v1.1`, `/api/pixelfed`), per M7.
- **W2 PeerTube `/api/v1`.**
  - W2.1 Read-only, for NewPipe: `/api/v1/config`, `/videos` (sorts, local), `/videos/{id}` with `files[]`,
    `/videos/{id}/comment-threads`, `/accounts/{h}`, `/video-channels/{h}/videos`, `/search/*`. The hard part is the
    media shape: `files[]` per resolution (PieFed stores one file; no HLS).
  - W2.2 Login and account: `oauth-clients/local`, `users/token` (password and refresh grants, OTP header), `users/me`,
    subscriptions, history, notifications, comments; the official app's version gate (D4).
  - W2.3 Upload: `videos/upload-resumable` into the existing media pipeline.
- **W3 Lemmy `/api/v3`** (D3): either a renaming serialiser over the W0.1 read layer plus an alias host whose nodeinfo
  says `lemmy`, or upstream PieFed support into Jerboa's API library (MV-GH/LemmyBackwardsCompatibleAPI), which needs no
  server work. v4 waits for Lemmy 1.0.
- **W4 gpodder.net sync** for podcast apps: `/api/2/auth`, subscriptions, episode actions.

### 2.3 Order (proposed, D1)

W0.1 and W0.2 → W1.1 (login, the first flip in the harness) → W2.1 in parallel with W1.2 (separate surfaces, no shared
files beyond W0) → W1.3 → W2.2 → W3 (per D3) → W2.3 → W1.4 → W4.

## 3. Rules every API follows

- Same visibility as the web and alpha: blocks, bans, NSFW/NSFL preferences, private instance and per-domain login
  (the login gate stays deny-by-default; each API's routes are listed there deliberately), local-only, deleted and
  removed content, moderation state.
- Writes through `app/shared/` only, so federation, notifications, rate limits and spam checks apply unchanged.
- Rate limiting per token and per IP, as the alpha API's.
- No new secrets in logs; tokens hashed at rest.
- Error shapes per API (each family has its own; apps parse them).
- Deny-by-default for unimplemented endpoints: a clean 404 in the family's error shape, never a 500.

## 4. Testing

- Unit: every route at 100% line and branch, through the Flask test client, plus contract tests of each entity's
  required fields against the strictest decoder found in W1.0 (Tusky's non-null fields), so a missing field fails here
  instead of crashing an app.
- Harness: `interop/run_remote.sh apps`, the gate of every phase. A phase is done when its rows pass in two
  consecutive cold runs.
- The conformance gate and the federation interop suite stay green: these APIs must not change federated behaviour.

## 5. Risks

- W0.1 is a refactor of the most-used API: done as a strangler, alpha first, its existing tests unchanged.
- Apps assume Mastodon's or PeerTube's semantics (ids as strings, pagination by `max_id`, media processing states);
  each mismatch shows up only when the app runs, which is what the harness is for.
- Identity tricks (D3, D4) can mislead crawlers and statistics sites if served on the main host; hence per-host only.
- The remote harness host has shown unexplained crashes; a failure there is diagnosed, never retried away.

## 6. Decisions for the owner

- **D1. Order.** Mastodon first (largest reach, shared OAuth), PeerTube read-only alongside from W1.2. Or PeerTube
  first (NewPipe needs no login: quickest visible win). Recommendation: Mastodon first.
  **Ruled 2026-11-07: Mastodon first**, PeerTube read-only alongside Mastodon's reading phase (the order in §2.3).
- **D2. The shared read layer (W0.1) before anything else**, accepting a refactor of `/api/alpha` with no visible
  change, or each new API querying on its own and copying the rules. Recommendation: the shared layer.
  **Ruled 2026-11-07: the shared layer first** (W0.1), alpha rebuilt on it with its test suite unchanged.
- **D3. Jerboa.** (a) Lemmy v3 subset plus an alias host whose nodeinfo says `lemmy`; (b) upstream PieFed support into
  Jerboa's API library and no shim; (c) leave Jerboa. Recommendation: (b), then (a) if upstream declines.
  **Ruled 2026-11-07: a v3 shim, on the main host, answering Jerboa by name**: "we'll do a similar hard-coding for
  their client that they're doing." Jerboa sends `User-Agent: Jerboa` on every request, nodeinfo included
  (`jerboa/app/src/main/java/com/jerboa/api/Http.kt:62,79-81`). So nodeinfo answers `software.name: lemmy` with a 0.19.x
  version (its library picks its API version from it) to that agent only, and `piefed` to everyone else. The nodeinfo
  cache key gains the agent's class and the response sends `Vary: User-Agent`, so neither answer is served to the
  other. W3 builds the `/api/v3` subset (renamed fields over the shared read layer); W0.3 becomes this per-client
  identity rather than per-host.
- **D4. The PeerTube app's version gate.** It refuses a `serverVersion` below 7.2.0 in `/api/v1/config`. Report a
  PeerTube-compatible version there (only in that endpoint, which only PeerTube clients read), or leave the official
  app out and serve NewPipe and Fedilab only. Recommendation: report it, documented as the API level served.
  **Ruled 2026-11-07: report the PeerTube API level** PieFed implements in `/api/v1/config` `serverVersion`, to every
  client (only PeerTube clients read it), documented as the API level served, not PieFed's version.
- **D5. Spec rulings M2-M8** for the Mastodon mapping (timeline content, titles, where a status goes, votes, token
  lifetime, Pixelfed's own app, phase order).
  **Ruled 2026-11-07**, recorded in the spec's §6: M2 home = people only, communities as lists; M3 bold title, body,
  link, community mention; M4 a mentioned community, else microblogs; M5 favourite = upvote; M6 tokens never expire,
  revocable; M7 spike Pixelfed's app after phases 1-3 and build what it needs; M8 one phase per branch.

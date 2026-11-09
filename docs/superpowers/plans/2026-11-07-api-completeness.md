# API completeness: as much of the fediverse's client apps as PieFed can serve

Date: 2026-11-07
Status: decisions D1-D5 ruled 2026-11-07 (§6). W0.1, W0.2 and W1.1 are built (2026-11-08); next is W1.2, reading.

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
  **Divergences kept in step 4, for the owner to rule on.** `get_user` (one person, by `person_id` or `username`) and
  `get_user_list` (the person list and search) read through `app/reads/users.py` (`person_by_id`, `person_named`,
  `refuse_hidden_person`, `person_listing`); `get_user_details` is the caller's own account (`authorise_api_user` and
  its view) and has no read to move. Line numbers are of `app/api/alpha/utils/user.py` at b997abfae unless named.
  1. Deleted people: a `username` lookup refuses a deleted person (59); a `person_id` lookup finds them (45, and
     `user_view`'s `db.session.get`, views.py 453-461) and their profile shows them deleted. (`Lookup.finds_deleted`,
     `BY_NAME`, `BY_ID`)
  2. Blocks: neither the lookups nor the person list apply any of the viewer's blocks (people, instances, banned
     instances: 41-90, 113-135), where the post and reply listings drop blocked people and instances
     (`app/reads/posts.py` 192-207, `app/reads/replies.py` 66-73). A blocked person is found and listed.
  3. Banned and unverified accounts: a Local listing leaves them out (114-117); an All listing (119) and both lookups
     find them. (`People.scope`)
  4. An address search (`q` with an `@`) matches `ap_id` only (124), which local accounts do not have, so it never
     finds a local account, `name@this.domain` included.
  5. Against the web's people directory (`app/instance/routes.py` 161-170), the API's list does not honour
     `searchable` (an account that opted out of the profile list is listed and found by search; see the security
     sweep below: FIXED d68b68714), and lists banned accounts (All), bots and silenced instances' accounts. Those
     three unchanged.
  Same in both and unchanged: an account hidden here is `Not found` by id and by name and is left out of the list
  (`hidden_here`, signed in or not); a bare name is the storefront's account, a hosted domain's name its own.
  tests/test_local_name_lookups.py allows the remote-name lookup (`func.lower(User.user_name) == name`) in alpha's
  `get_user` only, so that condition is still built there and handed to `person_named`; a later API that looks up a
  remote name needs its own ALLOWED entry, or the entry moved to `app/reads/users.py` (the owner's call: the test is
  not edited in a refactor).
  **Security sweep in step 4** (probe tests run against b997abfae's behaviour, all passing, i.e. each leak present).
  The owner ruled all four fixed, each its own commit, the API answering no more than the web shows; the gates are in
  `app/reads/users.py`, tests in tests/test_user_reads_privacy.py:
  1. `private_mods`: a person's `moderates` (views.py `cached_modlist_for_user`/`shown_modlist`, 1681-1711) names a
     community whose moderators are private, to anyone, signed in or not; the web profile hides it from all but admins
     and the person (`app/user/utils.py` 417-419). The community's own mod list (`cached_modlist_for_community`,
     views.py 1667-1678) ignores `private_mods` too (outside step 4). Likely a leak. **FIXED 9b069476f**
     (`moderates_hidden_from`; `moderators_hidden`: the community page names no private moderators,
     `app/community/routes.py` 522-523, and only its moderator page names them, to its moderators and an admin of
     communities, 1838; asked after the shared cache).
  2. Private communities: `moderates` names a private community the viewer is not a member of (name, title, icon),
     anonymous or not, while the community list leaves it out (`app/api/alpha/utils/community.py` 103-118). The web
     profile names it too, so this may be by design. **FIXED e5b5a86f0** (owner ruling: by membership,
     `Restrictions.private_community_ids`; the person and admins still see it; the web profile is unchanged).
  3. `searchable`: `get_user_list` (search type Users) lists and finds an account with `searchable = False`
     (User.searchable: "whether their profile is visible in profile list"); the web directory leaves it out.
     **FIXED d68b68714** (`person_listing` takes the viewer id; listed to an instance-wide 'administer all users'
     holder only, as `app/instance/routes.py` 159-165; lookups by id and name unchanged).
  4. Counts: `post_count`/`comment_count` (stored on the user row, `User.recalculate_post_stats`, models.py
     2722-2730, and the increments) count posts and comments in private communities, followers-only and local-only
     ones too, so a profile shows `post_count: 1, comment_count: 1` beside empty posts and comments to an anonymous
     viewer. A count, not content; Top sorts the person list by it. **FIXED 69ce7d909** (`listable_counts`: stored
     counts for the person and admins, else the listings' privacy predicates; one statement per person view, one per
     person-list page. Still open: Top sorts by the stored count, and the web profile shows the stored counts).
  Checked and not leaking: a person's posts and comments with `include_content` leave out a private community's to a
  non-member (probe; visibility and local-only go through `app/reads` as in steps 2 and 3), and local-only and
  off-domain accounts are `Not found` by id and by name and left out of the list (tests/test_reads_users.py).
- **W0.2 One OAuth 2 authorization server** (Authlib, already a dependency): app registration, authorization code
  with PKCE, out-of-band codes, refresh, client credentials, revocation, a settings page listing authorised apps.
  Mastodon, Pixelfed and Mbin-style clients use it; PeerTube's password grant and Lemmy's JWT login sit beside it on the
  same token table. Tokens stored hashed, scoped, revocable; a banned or deleted user's die with them.
  **DONE 2026-11-08** on branch `feat/oauth-server` (off `feat/shared-read-layer`), package `app/oauth_server`
  (`app.oauth` is the sign-in client, so not that name), scopes in `app/oauth_scopes.py`:
  1. bd9e70967 tables `oauth_app`, `oauth_code`, `oauth_token` (migration f6c2a8e4b1d7): SHA-256 digests only; codes
     10 min, single use; tokens never expire (M6); cascade on app or user delete.
  2. 6cca17644 + 5dbb6c758 `POST /api/v1/apps`, Mastodon's shape; 422 "Validation failed"; redirects exact, no
     whitespace or control characters, no javascript/data/vbscript/file/blob, no fragment; website http(s) only.
     7d030ec0a open on a private instance.
  3. 78601c6b1 + fa6ff997f `/oauth/authorize`: web login (password or passkey) and back, consent page (CSRF,
     frame-denied, no-store), errors shown here until the app and redirect check out, then sent back with `state`;
     PKCE S256 only, required of an app without its secret; OOB shows the code.
  4. cea733dcb + e8ec0c936 `POST /oauth/token`: authorization_code, refresh_token (rotating; the old token claimed
     atomically first; the app's secret required), client_credentials (read only, secret required); no password
     grant; form or JSON; Basic or body secret.
  5. c15c7e140 `POST /oauth/revoke` (RFC 7009) and `/.well-known/oauth-authorization-server` (RFC 8414).
  6. 1d4174492 + 517ddfd0c `require_oauth(scope, user_required=True)` in `app/oauth_server/protect.py`: Mastodon's
     401/403/422; the person must still be able to sign in (the alpha API's checks: not remote, unverified, banned,
     deleted, lapsed, nor a password change since); last use recorded hourly. The login gate reads no OAuth token: a
     `require_oauth` route keeps its own gate and refuses an app's own token on a private instance (an earlier version
     let a person's token through the gate to the alpha API, which read it as nobody: any scope could read).
  7. 658e97695 `/user/settings/apps`, "Authorised apps" in the settings menu, with revoke.
  2533291f3 the routes classified for the repo's sweeps (onion session-exempt, `app_id`, public metadata).
  Choices the spec left open, for the owner: `push` is accepted and grants nothing yet (Tusky asks for it); admin
  scopes are refused; tokens are not bound to the domain they were issued on (as alpha tokens) — **ruled 2026-11-08: bind them**, done in ec88a5321 (codes and tokens are exchanged, refreshed, revoked and used only on the domain they were issued on; migration a7d3e9b2c4f1 gives older rows the default domain); `require_oauth` has
  no route yet (W1.1 adds them). Six findings from four automated security reviews, each fixed in its own commit (above).
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
    **Inventory 2026-11-08 (Phanpy, Elk, Mastodon Android; sources read, not run):**
    - **Version:** no app parses `version` into a feature except Phanpy, and only for `@mastodon/*` features that also
      need nodeinfo `software.name` = `mastodon` (PieFed says `piefed`, so they stay off whatever the version); its
      basic features (lists, filters, bookmarks, edit, pinned) test only that `version` lacks "pixelfed". Elk checks
      only for `+glitch`; Mastodon Android never parses it. So `4.0.0` changes nothing in these apps today.
    - **`api_versions.mastodon`** is what gates: >= 2 switches Mastodon Android and Phanpy to grouped notifications
      (`/api/v2/notifications`), >= 3 PKCE in Mastodon Android, >= 7 quotes. PieFed sends none (0): v1 notifications.
    - **Paging:** every app follows `Link: rel="next"` (masto.js in Phanpy and Elk, HeaderPaginationRequest in
      Mastodon Android): no Link header, no second page. Phanpy's tag timeline sets `max_id` itself; new-post checks
      send `since_id`/`min_id`.
    - **Fatal:** `verify_credentials` at login (all three). Tolerated: preferences, push, markers, filters, custom
      emojis, nodeinfo; a timeline error empties that view only.
    - **Required (Mastodon Android drops an invalid status from a timeline page; context and search fail whole):**
      Status `id, uri, created_at, account, visibility, spoiler_text, media_attachments, mentions, tags, emojis`;
      Account adds `display_name`; Mention `id, username, acct, url`; Tag `name, url`; Attachment `id, type, url`
      (+`preview_url` for video); Card `url, title, description`; Poll `id, own_votes, options, emojis`; Notification
      `id, type, created_at, account`; Marker all. Elk compares `reblog` and `in_reply_to_id` to `null` strictly: send
      them as null, never omit. Phanpy needs `mentions` and `emojis` arrays. Elk reads
      `configuration.urls.streaming` without a check: send `configuration.urls`.
  - W1.1 Login: OAuth (W0.2), `/api/v2/instance` and a full v1, `verify_credentials`, accounts.
    **Done 2026-11-08** (branch feat/mastodon-login), in `app/mastodon/`:
    1. 86b522f64 `GET /api/v1/accounts/verify_credentials` (read:accounts; with `source`), `/accounts/:id` and
       `/accounts/lookup` (no token needed on a public instance; a person's on a private one). Tusky's required
       fields always set; `acct` bare for the requested domain; RFC 3339 times; absolute media URLs.
    2. 87be17ec6 `/api/v2/instance` and a complete `/api/v1/instance`: version `4.0.0 (compatible; PieFed …)`, 5000
       characters, 4 attachments, MAX_CONTENT_LENGTH as the media limit, per-domain branding, no streaming URL.
    3. b4168c713 + 4219faf69 `PATCH /api/v1/accounts/update_credentials` (form, multipart or JSON; numbered or listed
       `fields_attributes`; images saved first, nothing changed on a 422) and `GET /api/v1/apps/verify_credentials`.
       03e097a96 a field key must be the whole numbered name. bd140a636 CORS allows PATCH.
    4. Harness: 745c15e25 + 3fa89aee8 Phanpy and Elk (web) and Tusky and Mastodon (Android) are driven, not probed,
       through PieFed's login and consent; view and post expected unsupported until W1.2/W1.3. Two consecutive cold
       runs, 21/21 apps as the matrix says. On the emulator Chrome trusts only its own roots plus user CAs, so the
       harness root is a user CA too; the system WebView refuses a system CA's certificate for a reserved name
       (`ERR_CERT_NON_UNIQUE_NAME`), so Tusky logs in with "Login with Browser" and its WebView login goes untested.
    Moshidon, PixelDroid, Fedilab and Pixelfed stay probes (their logins pass as probes).
    Gates: suite 31957 passed, floors 140 and changed-line (base 1f90b5426) green, after ec88a5321.
    Open for the owner: `push` (Web Push subscriptions; accepted, grants nothing; Elk asks for it), admin scopes
    (refused outright today, so an app asking for one cannot log in; or accept and grant nothing), and the
    `4.0.0` version claim, which no harness validates and which apps (Phanpy) use to choose features: a survey of the
    harness apps' version thresholds is proposed before W1.2.
  - W1.2 Reading: timelines, statuses and context, notifications, search, markers, custom emojis.
    **Built 2026-11-08** (branch feat/mastodon-reading off blentz 872aba94d), in `app/mastodon/`:
    1. a2a3b65d8 + 5c531097e `ids.py`: a status id is a snowflake, the second since 2000 it was posted above one bit
       for the table (post or comment) and the whole 31-bit row id: unique by construction, ordered by time, decoded
       by primary key. (The first version kept 15 bits of the row under the millisecond, which a remote server's
       chosen `published` could collide: security review.)
    2. c186f7cdb `statuses.py`: Status per M3/M5 (bold title, body, link, community mention; favourites = upvotes);
       every decoded field present, nulls null; a Page loads votes, bookmarks, boosts and author counts once.
    3. 7dcd5c60b the read layer's post and comment listings take `person_ids` (home, M2).
    4. ea932420d + c94f0dcf6 `paging.py`/`timelines.py`: public (local, remote, only_media), tag, home (people
       followed and oneself, posts and comments), list/:id (a joined community), accounts/:id/statuses,
       statuses/:id and /context; pages in (posted_at, table, row id) order on the posted_at index, merged across
       tables, Link headers. A bound naming a row the viewer may not read uses its id's second, never the row
       (security review: an existence/timestamp oracle).
    5. 922eeebda the RSL licence joins a response's Link header (OkHttp reads only the last Link header).
    6. c014f68f0 + 7b022c8dd notifications (v1: status, mention, follow, follow_request), v2 search, lists,
       custom emojis, preferences, filters, followed tags, markers (empty), trends and suggestions (empty).
    7. 52cd9b9f1 harness: Phanpy and Elk open the c/apps list, Tusky and Mastodon's app search for the seed; the
       probed Mastodon/Pixelfed apps' view passes.
    8. the probe counts Mastodon's 401 on the (private) harness instance as served; status_id/list_id classified.
    Merged to local blentz 2026-11-08 at the owner's word, before its two cold harness runs and the full gates
    finished; their results are recorded below when in.
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

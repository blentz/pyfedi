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

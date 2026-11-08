# Mastodon client API: Mastodon and Pixelfed apps on PieFed

Date: 2026-11-07
Status: owner rulings M1-M8 given 2026-11-07 (§6); part of the API-completeness plan
(`docs/superpowers/plans/2026-11-07-api-completeness.md`, workstream W1). Nothing here is built yet.

The client-app harness (`interop/apps/README.md`, 2026-11-07) checked the top apps of five platforms against PieFed.
The Lemmy apps that speak PieFed's own API all work: Voyager, Blorp and Photon on the web, and Voyager, Thunder and
Summit on Android, each logging in, viewing and posting. So does AntennaPod with PieFed's podcast feeds. Every Mastodon
and Pixelfed app fails at its first call: there is no OAuth app registration, so no login. This spec is what it takes
to let them in.

## 1. Goal

A PieFed user signs in to PieFed from a Mastodon app (Tusky, the official Mastodon app, Moshidon, Phanpy, Elk, Ice
Cubes, Ivory) or a Pixelfed app (PixelDroid, Fedilab, possibly the official Pixelfed app, M7). They read what they
follow, and they post, reply, favourite and boost, with images. Proved by the harness: each of those apps driven for
real, as the Lemmy apps are now. The matrix entries flip from `unsupported` to `passed` one phase at a time.

### Out of scope (proposed)

- PeerTube's REST API (the PeerTube app, TubeLab, NewPipe) and Lemmy's `/api/v3` (Jerboa): separate decisions (M1).
- Web Push (`/api/v1/push/subscription`): apps then poll for notifications.
- Streaming (`/api/v1/streaming`): apps fall back to polling.
- Admin, filters v2, lists, announcements, trends, polls (create), scheduled statuses, edits history, quote posts.

## 2. Current state (harness run 2026-11-07)

| Call an app starts with | PieFed answers |
|---|---|
| `GET /api/v1/instance` | 200, a stub: title, uri, stats, registrations, approval_required (`app/activitypub/routes.py`) |
| `GET /api/v2/instance` | 404 |
| `POST /api/v1/apps` | 404 |
| `GET /oauth/authorize`, `POST /oauth/token` | 404 |
| `GET /api/v1/accounts/verify_credentials` | 404 |
| `GET /api/v1/timelines/home`, `/public` | 404 |

What exists to build on:

- Authlib (`Authlib~=1.8.0`, now used as an OAuth client for Google, Mastodon and Discord sign-in) ships an OAuth 2
  authorization server for Flask with PKCE.
- The alpha API's views (`app/api/alpha/`) already compute everything a Mastodon timeline needs: post lists by feed
  type and sort, a post's replies, votes, saves, uploads. A Mastodon layer is a second serialiser over the same
  queries, as the alpha API is over the models.
- Microblog posts (`Post.microblog`, the local `microblogs` community, spec 2026-10-14) are already PieFed's
  Mastodon-shaped content: title-less posts by people, which Mastodon shows natively.
- The login gate (`app/login_gate.py`) is deny-by-default; the new routes join it like the alpha API's.

## 3. Shape of the API

### 3.1 OAuth 2 (phase 1)

- `POST /api/v1/apps`: registers a client (name, redirect URIs, scopes, website); returns `client_id` and
  `client_secret`. Open to anyone, rate-limited per IP, as Mastodon's is.
- `GET /oauth/authorize`: PieFed's own login (with its 2FA, passkeys and the private-instance gate), then a consent
  page naming the app and scopes. Authorization code grant with PKCE (S256); `urn:ietf:wg:oauth:2.0:oob` shows the code
  to copy, for the apps that use it.
- `POST /oauth/token`: `authorization_code`, `refresh_token`, and `client_credentials` (app-level token, read only).
  No password grant (Mastodon dropped it).
- `POST /oauth/revoke`; a user settings page listing authorised apps, with revoke.
- Tokens: a new table, stored hashed (SHA-256), with scopes, app and user, last use; a banned or deleted user's tokens
  die with them. Scopes: `read`, `write`, `follow`, `profile` and their `:sub` forms, checked per route.
- `GET /.well-known/oauth-authorization-server` (Mastodon 4.3+ apps discover the endpoints there).

### 3.2 Instance and account (phase 1)

- `GET /api/v2/instance` and a complete `/api/v1/instance` (configuration: status length, media limits and types,
  registrations, contact account, rules). Apps size their composer and uploads from it.
- `GET /api/v1/accounts/verify_credentials`, `GET /api/v1/accounts/:id`, `GET /api/v1/accounts/lookup`,
  `PATCH /api/v1/accounts/update_credentials` (display name, bio, avatar, header).

Phase 1 done = every probed app's `login` passes, and the harness drives Tusky, Mastodon, Phanpy and Elk through it.

### 3.3 Reading (phase 2)

- `GET /api/v1/timelines/home`, `/public` (`local`, `remote`), `/tag/:tag`; `GET /api/v1/statuses/:id`,
  `/:id/context`; `GET /api/v1/accounts/:id/statuses`; `GET /api/v1/notifications`; `GET /api/v2/search`;
  `GET /api/v1/custom_emojis`; `GET /api/v1/markers` (read positions). Pagination by `max_id` / `since_id` /
  `min_id` with `Link` headers, ids as strings.
- The mapping of PieFed's content to statuses is M2-M5.

### 3.4 Writing (phase 3)

- `POST /api/v1/statuses` (text, `in_reply_to_id`, `media_ids`, `sensitive`, `spoiler_text`, `visibility`,
  `language`); `DELETE` and `PUT` (edit) on `/api/v1/statuses/:id`.
- `POST /api/v2/media` (images; video and audio where the user may upload them, through the existing media pipeline,
  answering 202 while it probes); `PUT /api/v1/media/:id` (alt text).
- `/favourite`, `/unfavourite`, `/reblog`, `/unreblog`, `/bookmark`, `/unbookmark` on statuses.
- `POST /api/v1/accounts/:id/follow`, `/unfollow`, `/block`, `/mute`; `GET /api/v1/follow_requests`.

### 3.5 Pixelfed apps (phase 4, after a spike)

Pixelfed's apps are Mastodon clients with extras: `/api/v1.1/` and `/api/pixelfed/v1/` routes (discover, collections,
stories) and, for the official app, `GET /api/nodeinfo/2.0.json`, which it reads before logging in. A spike runs
PixelDroid and the official app against phase 1-3 and lists what each still needs (M7).

## 4. Harness

Each phase adds real drivers for the apps it unlocks (`interop/apps/test_android_apps.py`, `test_web_apps.py`),
replacing their probes, and removes their expectations from `matrix.json`; the gate's "now passes" error is the
reminder. Unit tests cover every route at 100% line and branch, as for the alpha API.

## 5. Security notes

- Redirect URIs are matched exactly against the registered ones; no open redirect from `/oauth/authorize`.
- PKCE is required for public clients (no secret), optional for confidential ones, as RFC 9700 advises.
- Codes are single use and expire in 10 minutes; access tokens never expire unless revoked (Mastodon's behaviour,
  which apps rely on), refresh tokens are accepted where an app asks for them.
- The consent page is CSRF-protected and frame-denied. Tokens are never logged.
- Scopes are enforced per route; `read` cannot post. A token on a private instance passes the login gate only for
  its own user.
- The rate limiter applies to app registration and to the token endpoint.

## 6. Questions for the owner

- **M1. Scope.** Mastodon client API only (Mastodon and Pixelfed apps), or also PeerTube's REST API (the PeerTube app,
  TubeLab, NewPipe) and Lemmy's `/api/v3` (Jerboa)? Recommendation: Mastodon only. Lemmy users already have three
  working apps, two on Android. PeerTube's API is a second large surface for a smaller audience.
  **Superseded 2026-11-07 by the owner's goal, "support as much of the fediverse as we possibly can"**: the
  API-completeness plan takes PeerTube's API and a Lemmy v3 subset too, as their own workstreams.
- **M2. What a Mastodon app's home timeline holds.** (a) posts and replies by people the user follows, plus posts in
  communities they joined, each as a status by its author; (b) people only, communities left to a "list" per community.
  Recommendation: (a), so the timeline matches PieFed's own home feed.
  **Ruled 2026-11-07: (b) people only.** Home holds posts and replies by accounts the user follows; each community the
  user joined is a Mastodon list (`/api/v1/lists`, `/api/v1/timelines/list/:id`), so communities stay reachable
  without turning the person-centred home timeline into a forum feed.
- **M3. How a titled community post shows as a status.** Mastodon has no titles. Recommendation: the title in bold as
  the first line, then the body, then the link, the community as a mention (`@apps@piefed.test`) so a reply stays in
  that community. This is how Mastodon renders Lemmy posts today, which its users know.
  **Ruled 2026-11-07: as recommended** (bold title, body, link, the community as a mention).
- **M4. Where a status written in a Mastodon app goes.** (a) the user's microblogs (the local `microblogs`
  community), as a title-less post; (b) a community when the status mentions one, else microblogs. Recommendation:
  (b), which is what a Mastodon user federating to Lemmy already does.
  **Ruled 2026-11-07: (b)**: a status mentioning a community is a post there (title derived as for an inbound Mastodon
  note), otherwise a title-less post in the user's microblogs.
- **M5. Votes.** Favourite = upvote, unfavourite = remove the vote; downvotes have no Mastodon form and are not shown.
  Boost = PieFed's boost (Announce). Agree?
  **Ruled 2026-11-07: agreed** (favourite = upvote, `favourites_count` = upvotes, downvotes not shown, boost = boost).
- **M6. Tokens.** Never expiring unless revoked (as Mastodon, which apps assume), listed and revocable in settings.
  Or a lifetime (e.g. a year) that apps then hit as a sudden logout. Recommendation: never expiring, revocable.
  **Ruled 2026-11-07: never expiring**, revoked by the user (settings lists authorised apps), a password change, a ban
  or the account's deletion.
- **M7. Pixelfed's own app.** Accept that it may need Pixelfed-only routes (phase 4 spike), or take PixelDroid and
  Fedilab as the Pixelfed apps and leave the official one `unsupported`?
  **Ruled 2026-11-07: spike, then build what it needs**: after phases 1-3 the app runs in the harness, its
  Pixelfed-only calls are listed and built, with per-client identity (as for Jerboa, plan D3) if it gates on the
  software name.
- **M8. Order.** Phase 1 (login) alone first, merged and harness-proved, then 2, then 3, each its own branch, as the
  native-federation steps were. Agree?
  **Ruled 2026-11-07: one phase per branch**, each merged when the harness flips its rows in two consecutive cold runs
  and the full gates pass.

## 7. Also found by the harness (not part of this API)

- **The harness runs a private instance.** A fresh site row is private (`Site.private_instance` defaults to true), so
  every logged-out app call gets 401 and the harness checks the logged-in path only. Apps also browse a public instance
  logged out (Voyager, Thunder, Summit open on a feed). A second harness pass with the site public would cover that.
- **iOS Mlem, unverifiable here.** Mlem tries a Lemmy connection before a PieFed one, and PieFed's `/api/v3/site`
  stub (upstream's, for Lemmy's instance crawlers) answers a full Lemmy site document. If Mlem decodes it as Lemmy, it
  logs in with `/api/v3/user/login`, which 404s. iOS cannot run in the harness; worth a manual check with Mlem before
  touching the stub, which crawlers rely on.

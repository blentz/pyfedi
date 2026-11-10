# Pixelfed's own app on PieFed (W1.4 spike)

Date: 2026-11-10
Status: spike written; owner rulings X1-X4 open (§5). Part of the API-completeness plan
(`docs/superpowers/plans/2026-11-07-api-completeness.md`, W1.4), per ruling M7 of
`2026-11-07-mastodon-client-api-design.md`: "spike, then build what it needs ... with per-client identity (as for
Jerboa, plan D3) if it gates on the software name". Nothing here is built yet.

## 1. The apps

| App | Source read | What it speaks | Today |
|---|---|---|---|
| Pixelfed (official, React Native) | `pixelfed/pixelfed-rn` v2.0.1, commit da4bcf7 (GitHub release `app-release.apk`) | Mastodon's API plus ~30 Pixelfed-only routes | a `mastodon` probe; refuses PieFed at its first call |
| PixelDroid | F-Droid `org.pixeldroid.app` 1.0.beta42 (its GitLab refused cloning) | Mastodon's API | a `mastodon` probe |
| Fedilab | `tom79/Fedilab` commit 1dfcb7f | Mastodon's API, adapting to nodeinfo's software | a `mastodon` probe |

PixelDroid and Fedilab speak the Mastodon API that W1 serves; the official app is the one M7 is about.

## 2. What the official app needs

- **It refuses any server that is not Pixelfed.** Before login it reads `GET /api/nodeinfo/2.0.json` (Pixelfed's own
  path, not the standard `/nodeinfo/2.0`) and refuses unless `software.name` is `pixelfed` and `software.version` is
  0.12.3 or later (`src/requests.ts:150-185`). It sends no User-Agent of its own (React Native's okhttp default), so
  it cannot be told apart by agent; the path itself is asked by Pixelfed clients alone.
- **Login** is Mastodon's: `POST /api/v1/apps`, `/oauth/authorize` in the browser, `/oauth/token`, then
  `GET /api/v1/accounts/verify_credentials?_pe=1` (W1.1 serves all of it). Scopes asked: `read write follow push`
  (and `admin:read admin:write`, which W1's admin scopes handle).
- **Pixelfed's statuses (`_pe=1`).** The home, public and profile feeds ask `_pe=1`, Pixelfed's extended status. The
  profile grid shows only statuses whose `pf_type` is `photo`, `photo:album` or `video`
  (`(tabs)/profile.tsx:91-95`), the feed picks its player by `pf_type`, and posts show `liked_by` and `reply_count`.
  Without them a person's profile is empty.
- **Pixelfed-only routes**, by the screen that calls them (`src/lib/api.ts`):

| Route | Screen | Built as |
|---|---|---|
| `GET /api/v2/config` | settings | PieFed's limits in Pixelfed's shape |
| `GET /api/v1.1/compose/settings` | camera (posting) | the media limits and types, the caption length |
| `POST /api/v2/media` | camera (posting) | already served: W1's media upload (`app.mastodon.media`) |
| `GET /api/v1.2/stories/carousel` | home | empty: PieFed has no stories |
| `POST /api/v1.1/stories/seen`, `stories/self-expire`, `v1.2/stories/*` | stories | refused: none |
| `GET /api/v1.1/discover/posts/trending`, `posts/network/trending`, `posts/hashtags` | explore | PieFed's top image and video posts, and its trending tags |
| `GET /api/v1.1/accounts/username/<name>` | profile | the account by name |
| `GET /api/v1.1/accounts/mutuals/<id>` | profile | the accounts both follow |
| `POST /api/v1.1/report` | report | PieFed's report (app.shared) |
| `GET /api/pixelfed/v1/app/settings` | settings | defaults |
| `GET /api/v1.1/nag/state`, `push/*` | notifications settings | push off: Pixelfed's own push relay is not PieFed's (W1.3 push is Mastodon's Web Push) |
| `GET /api/v1.1/collections/self` | collections | empty |
| `api/v1.1/direct/*` | chats | per X2 |
| `DELETE /api/v1.1/accounts/avatar` | settings | the avatar removed |
| `GET /api/v1/mobile-app/servers/*.json` | server picker | not served: it lists pixelfed.org's servers |

## 3. How it would be built

- Identity per X1, on that one path.
- `_pe=1` adds Pixelfed's fields to W1's Mastodon status (`app.mastodon.statuses`): `pf_type` from the post's media
  (an image post `photo`, a gallery `photo:album`, a video `video`, anything else `text`), `reply_count`, `liked_by`
  (the count and the first liker's name), `local`. Mastodon's own fields stay as W1 makes them.
- Every route through `app/shared` and W1's reads, with the same visibility, blocks and limits.
- Errors in Pixelfed's (Mastodon's) shape: `{"error": "..."}`.

## 4. Harness

Per X4.

## 5. Decisions for the owner

- **X1. Identity.** The official app refuses PieFed unless `/api/nodeinfo/2.0.json` says `pixelfed`. Answer that path
  (and only it; the standard `/nodeinfo/2.0` and `2.1` stay PieFed's) as Pixelfed 0.12.6, the API level served, as D3
  does for Jerboa; or leave the official app unsupported. Recommendation: answer it: only Pixelfed's clients ask that
  path, so no crawler or statistics site is told anything new.
- **X2. Chats.** Pixelfed's direct messages (`/api/v1.1/direct/*`) map onto PieFed's chat; build them now, or answer
  them empty and leave chat to the web (no API of PieFed's other than alpha's serves it to apps yet). Recommendation:
  empty now, a later item: the core is browsing, posting photos, comments, likes, follows and notifications.
- **X3. Stories.** PieFed has none: the carousel answers empty and every story write is refused, or build stories.
  Recommendation: empty and refused; stories are a feature of their own.
- **X4. The harness.** (a) Drive the official app (GitHub release APK): log in as alice, see the seeded post, post a
  photo through its camera tab's picker; PixelDroid and Fedilab stay `mastodon` probes. (b) Drive all three.
  Recommendation: (a): PixelDroid and Fedilab ask only the Mastodon API, which Tusky's and Mastodon's own drivers
  already exercise.

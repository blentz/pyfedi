# Pixelfed's own app on PieFed (W1.4 spike)

Date: 2026-11-10
Status: spike written; owner rulings X1-X4 given 2026-11-10 (§5), X1 as recommended, X2-X4 otherwise. Built in
phases, one branch each (M8): W1.4a identity, Pixelfed statuses, the core routes and the official app driven; W1.4b
chats; W1.4c stories; W1.4d PixelDroid and Fedilab driven. Part of the API-completeness plan
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
| `POST /api/v1.1/report` | report | a post's, through PieFed's report (app.shared); an account is reported on the web, as PieFed's profile report is the web's alone |
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
  (always null: who voted is shown to moderators alone on PieFed, and the app takes null), `local`. Mastodon's own
  fields stay as W1 makes them.
- Every route through `app/shared` and W1's reads, with the same visibility, blocks and limits.
- Errors in Pixelfed's (Mastodon's) shape: `{"error": "..."}`.

## 4. Harness

Per X4.

## 5. Decisions for the owner

- **X1. Identity.** The official app refuses PieFed unless `/api/nodeinfo/2.0.json` says `pixelfed`. Answer that path
  (and only it; the standard `/nodeinfo/2.0` and `2.1` stay PieFed's) as Pixelfed 0.12.6, the API level served, as D3
  does for Jerboa; or leave the official app unsupported. Recommendation: answer it: only Pixelfed's clients ask that
  path, so no crawler or statistics site is told anything new.
  **Ruled 2026-11-10: say Pixelfed 0.12.6 on that path alone.** Found on the emulator: the app's 2.0.1 release (newer
  than its public source) reads `GET /api/v2.1/config` instead, before sign-in, and needs 0.14.4 or later; both
  Pixelfed-only paths now say 0.15.2, Pixelfed's current version, in the ruling's sense (the API level served).

- **X2. Chats.** Pixelfed's direct messages (`/api/v1.1/direct/*`) map onto PieFed's chat; build them now, or answer
  them empty and leave chat to the web (no API of PieFed's other than alpha's serves it to apps yet). Recommendation:
  empty now, a later item: the core is browsing, posting photos, comments, likes, follows and notifications.
  **Ruled 2026-11-10: build chats now** (phase W1.4b): Pixelfed's threads are PieFed's conversations and chat
  messages (app.chat), through the code the web's chat uses.
  Found on the emulator 2026-11-11: the 2.0.1 release uses Pixelfed's newer direct API (its server's
  DirectConversationController: `/api/v1.1/direct/conversations[/{id}[/messages|/read|/accept|/mute|/hide|/leave…]]`
  and `/api/v1.1/direct/unread_count`), with Inbox, Requests and Hidden tabs. PieFed's chat is one-to-one and
  text-only, with no requests, hiding or muting. **Ruled 2026-11-11:** build requests (the web chat too), build hide
  and mute, build media in messages; group chats stay refused (`/api/v2.1/config` says so). See §6.

- **X3. Stories.** PieFed has none: the carousel answers empty and every story write is refused, or build stories.
  Recommendation: empty and refused; stories are a feature of their own.
  **Ruled 2026-11-10: stories are microblog posts; clients are shown only those of the last 24 hours** (phase
  W1.4c). The carousel lists the accounts whose microblog posts with media from the last 24 hours the person may see;
  publishing a story makes such a post; a story is no longer listed after a day, and stays a post.

- **X4. The harness.** (a) Drive the official app (GitHub release APK): log in as alice, see the seeded post, post a
  photo through its camera tab's picker; PixelDroid and Fedilab stay `mastodon` probes. (b) Drive all three.
  Recommendation: (a): PixelDroid and Fedilab ask only the Mastodon API, which Tusky's and Mastodon's own drivers
  already exercise.
  **Ruled 2026-11-10: drive all three** (the official app in W1.4a, PixelDroid and Fedilab in W1.4d).

## 6. Chats (W1.4b, rulings X2 and 2026-11-11)

PieFed's `Conversation`, `conversation_member` and `ChatMessage` stay the model; every write goes through the code the
web chat and alpha's private messages use (`app.chat.util.send_message`, the inbox's ChatMessage arm), so a message
sent from the app, the web or alpha, or received over ActivityPub, is one thing.

**Per-member state** (new columns on `conversation_member`, one migration): `state` (`active` | `request`),
`hidden` (bool), `muted` (bool).
- *Requests.* When a message starts a conversation, its sender's membership is `active`; the recipient's is `request`
  unless the recipient follows the sender. Sending a message in a conversation, or Accept, makes one's own membership
  `active`. A request makes no notification (Pixelfed's own behaviour); it is counted in the app's `requests` and
  listed in the web chat's new Requests section, with Accept. PieFed's who-may-message-me settings still gate first.
- *Hidden* is the app's own filing: hide moves a conversation to its Hidden tab; one's own message or a new message
  from the other side brings it back. The web chat's list does not filter on it.
- *Mute* stops a conversation's chat notifications for that member, whatever sent the message (web, app, alpha,
  federation); the web chat's options page gets a mute toggle.
- *Leave* is PieFed's existing leave (`joined` false; the conversation is deleted once abandoned).

**The app's API** (`app/mastodon/direct.py`), Pixelfed's shapes (its DirectMessagePayloadService): a conversation is
`{id, type: 'direct', name: null, participants: [account…], participant_count, state, unread_count, muted, hidden,
last_read_message_id, last_message, created_at, updated_at}`; a message is `{id, conversation_id, sender_id, is_author,
type, text, media: [attachment…], meta: null, sensitive, in_reply_to_id: null, edited_at, created_at}`. Lists filter
`primary|requests|hidden` with an opaque cursor; messages page newest first by `max_id`, and `min_id` polls.
`unread_count` is `{primary, requests}`. A conversation started with more than one recipient is refused (422).
Unread is per message (`ChatMessage.read` for its recipient), as the web chat reads it; `read` marks messages up to
`message_id` (or all) read, with their notifications.

**Media** (W1.4b2): a `chat_message_file` link (message, file, order); up to 4 images per message, uploaded first
through `/api/v2/media` and named by `media_ids`; federated as the object's `attachment` (Image documents with
`mediaType`, `url`, `name` for the alt text) and taken from an incoming one's; the web chat shows them.
`/api/v2.1/config`'s `direct_messages.max_media_attachments` says 4.

**Phases.** W1.4b1: per-member state, the API for text, the web chat's Requests and mute, the app driven (alice
messages dora; dora's reply is read). W1.4b2: media in messages, both ways and on the web.


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
| PixelDroid | F-Droid `org.pixeldroid.app` 1.0.beta42; source read from the GitHub mirror `H-PixelDroid/PixelDroid` 7db32eb (its GitLab refused cloning) | Mastodon's API, plus Pixelfed's stories | driven (W1.4d, §8) |
| Fedilab | `tom79/Fedilab` commit 1dfcb7f (3.45.1; F-Droid `fr.gouv.etalab.mastodon`) | Mastodon's API, adapting to nodeinfo's software | driven (W1.4d, §8) |

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
| `GET /api/v1.2/stories/carousel` | home | the stories of §7 (W1.4c) |
| `POST /api/v1.1/stories/seen`, `stories/self-expire`, `stories/comment`, `GET v1.2/stories/viewers` | stories | §7; publishing is PixelDroid's (§8, S5) |
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
`/api/v2.1/config`'s `direct_messages.max_media_attachments` says 4. Built 2026-11-11:
- The web chat shows a picture stored here inline (on disk, or in `/static/media/` at a hosted domain's address, with
  no query, fragment or `..`: `File.stored_here`). A peer's is a link, so no remote server learns who read the chat
  until the reader opens it (the page already strips images from a message's text).
- An incoming picture naming any hosted domain's address is refused (security review 2026-11-11: it would be shown
  as ours, an `<img>` carrying the reader's cookies).
- A message gives no `edited_at`: `ChatMessage.edited_at` moves on any change of the row, its read flag's too.

**Phases.** W1.4b1: per-member state, the API for text, the web chat's Requests and mute, the app driven (alice
messages dora; dora's reply is read). W1.4b2: media in messages, both ways and on the web.


## 7. Stories (W1.4c, ruling X3 and rulings S1–S4 of 2026-11-11)

A story is a microblog post (`Post.microblog`, or a post in the local `microblogs` community) with a picture, posted
in the last 24 hours by the viewer or someone they follow, that the viewer may read (`readable`). Image and gallery
posts count, and a gallery shows its first picture. A video post does not: PieFed's video post points at a page, not
a file the app's player can play. Nothing new is stored for the story itself. The app (2.0.1) calls these
(Pixelfed's StoryApiV1Controller `carouselNext`, `viewed`, `delete`, `viewers`, `comment`):

- `GET /api/v1.2/stories/carousel` returns `{nodes: [{id: 'self:<id>' | 'pfs:<id>', username, username_acct,
  profile_id, avatar, is_author, stories: [{id, type: 'photo', url, overlays: [], duration: 10000, viewed,
  created_at, view_count (the author's own only)}], url, hasViewed}]`. The viewer's own node comes first, then the
  others, latest first. A story's id is its post's.
- `POST /api/v1.1/stories/seen {id}`: **S1, recorded, with no viewers list.** A `story_view` row (user, post,
  when) is written only by this call, never by reading the post elsewhere. It greys the story out for the viewer.
  `viewers` always lists nobody, and `view_count` is 0, so no author learns who looked or how many did.
- `POST /api/v1.1/stories/self-expire/<id>`: **S2, drop from the carousel.** It applies to the author's own story
  only. A `story_expiry` row removes it from every carousel; the post stays a post, everywhere else unchanged.
- `POST /api/v1.1/stories/comment {sid, caption}`: **S3, sent as a chat.** The caption and a link to the post go to
  the author as a chat message, through W1.4b's send with its request, mute, block and may-message rules. Only a
  story in the commenter's carousel, not their own, may be commented on.
- **S4, publish refused** until an app uses it. App 2.0.1 never calls its publish, and a photo posted as usual is a
  story for a day anyway. PixelDroid does publish: S5 (§8) builds `stories/add` and `stories/publish`; the v1.2
  publish stays unbuilt (404).

Rows in `story_view` and `story_expiry` matter for a day: each write deletes that table's rows older than two
days. Deleting a post deletes its rows (cascade).

## 8. PixelDroid and Fedilab (W1.4d, ruling X4 and rulings S5–S6 of 2026-11-11)

Both are driven in the harness: each logs in through OAuth, views dora's photo or the seeded post, and posts.

**Fedilab** reads nodeinfo first and picks its API from `software.name`: anything it does not know, `piefed`
included, gets Mastodon's API, which W1 serves. Its OAuth redirect is `fedilab://backtofedilab`. Nothing was built
for it.

**PixelDroid** speaks Mastodon's API and, without checking the server, Pixelfed's stories. At login it reads the
standard nodeinfo (`/.well-known/nodeinfo`, then the schema 2.0 document) and:

- warns, "This doesn't seem to be a Pixelfed instance, so the app could break in unexpected ways", where
  `software.name` lacks `pixelfed`;
- stops ("The API is not activated on this instance") where it does say pixelfed but `metadata.config.features.
  mobile_apis` is not true;
- stores the instance from `metadata.config` (site, uploader limits) when it has the site's url and name and a caption
  length, else from `/api/v1/instance`.

**S6, told Pixelfed by agent.** PixelDroid sends `User-Agent: PixelDroid` on every request. That agent alone is told
Pixelfed 0.15.2 on the standard nodeinfo (2.0 and 2.1), and the 2.0 document carries `metadata: {nodeName, software:
{homepage, repo}, config}`, the config being `/api/v2/config`'s. The repository stays PieFed's, so PixelDroid does not
treat the server as Pixelfed for its avatar upload (it uses Mastodon's `update_credentials`). Every other agent is told
PieFed, as before; the answer is cached per agent class and says it varies by agent.

**S5, stories viewed and published** (reversing S4, now that an app publishes):

- `GET /api/pixelfed/v1/stories/self-carousel` (Pixelfed's `selfCarousel`) returns `{self: {user: {id, username
  (acct), avatar, local, is_author: true}, nodes: [stories]}, nodes: [{id: 'pfs:<id>', user: {id, username,
  username_acct, avatar, local, is_author: false}, nodes: [stories], url, seen}]}`, each story `{id, pid, type:
  'photo', src, duration: 10 (seconds), seen, created_at}`, oldest first. The stories are §7's. Authors whose newest
  story is unseen come first.
- `seen` and `comment` take their fields from the query, as PixelDroid sends them, as well as from a JSON body.
- `POST /api/v1.1/stories/add` (multipart `file`) stores the photo as `/api/v2/media` does and answers `{code, msg,
  media_id, media_url, media_type: 'photo'}`. Pictures only, as Mastodon's media here.
- `POST /api/v1.1/stories/publish {media_id, duration, can_reply, can_react}` posts the person's own upload to their
  microblog with no words, as `POST /api/v1/statuses` would: a story for a day, and a post. Duration, replies and
  reactions are not kept: a story shows for ten seconds, and a reply to it is a chat (S3).

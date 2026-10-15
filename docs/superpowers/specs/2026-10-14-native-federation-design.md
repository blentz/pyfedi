# Native federation to Mastodon, PeerTube and Castopod

Date: 2026-10-14
Status: design approved in conversation; spec awaiting owner review

## 1. Goal

A user on Mastodon, PeerTube or Castopod follows a hell.cloud user or community and
sees its content in their own app as if a native account had posted it:

| Local content | Remote target | Native display means |
|---|---|---|
| A post in the local `microblogs` community | Mastodon and compatible microblogs | A status in the follower's home timeline: body text only (no duplicated title), attachments shown, hashtags and mentions live |
| An uploaded video in a `video_channel` community | PeerTube | A playable video listed under the subscribed channel, with the right duration and thumbnail |
| An uploaded audio episode in a `podcast` community | Castopod | An episode or episode announcement shown as Castopod shows its own (exact shape set by the Castopod spike, §9 step 5) |

Replies, likes and boosts from those apps come back into the post's PieFed thread.

### Owner rulings (2026-10-14)

- R1. Success means native display in the remote app (not only reachability by link or search).
- R2. Verification runs against local containers of each target app, not public servers.
- R3. Only video files uploaded to hell.cloud federate as PeerTube `Video`. Link-only video
  posts (YouTube, remote `.mp4` URLs) keep federating as today (`Page` + `Link`).
- R4. A local podcast is a community of kind `podcast`. Each post carrying uploaded audio is
  one episode, and the community actor is the podcast.
- R5. A PeerTube channel is a community of kind `video_channel`.
- R6. Media processing is limited to probing and one thumbnail. No transcoding.
- R7. Video and audio uploads have admin-set size limits. Only moderators of the
  `video_channel` or `podcast` community may upload into it.
- R8. Architecture: one serializer per object shape, used by both push and fetch (approach A).
- R9. 100% test coverage, line and branch, on every line this work adds or changes (§8.4).

### Out of scope

PeerTube redundancy (mirroring our videos), live streams, playlists, transcoding,
Castopod analytics (OP3), per-user storage quotas, and changing `kind` on a community
that already has posts.

## 2. Current state (what this changes)

- Every local post belongs to a community (`app/shared/post.py`, `make_post`).
- Local users can already post in the local `/c/microblogs` community. Followers-only posts there
  federate to followers only (Amendment B, commit `2992fb927`).
- `send_post` (`app/shared/tasks/pages.py`) builds a `Page` (or `Question` / `Event`), Announces it
  from a local community, and then sends a rewritten `Note` copy to the author's followers and mentions.
  That copy prepends `<p>title</p>` to the body.
- `post_to_page` (`app/activitypub/util.py`) builds the fetched object separately and serves a `Page`
  with `name`. The pushed and fetched objects therefore differ; for microblogs, Mastodon shows the fetched
  `Page` as a link card.
- Video uploads (mp4/webm/mov) exist behind `allow_video_file_uploads`. There is no size cap and no
  `MAX_CONTENT_LENGTH`. Uploaded video goes out as a `Link` attachment.
- There are no audio uploads, and no `Video`, `Audio` or `PodcastEpisode` object is ever sent.
- `microblogs` is special-cased by name: `is_local_microblogs` (`app/utils.py`) and
  `Post.is_microblog` (`app/models.py`).
- Inbound Castopod handling (`castopod_episode_url`, `fetch_castopod_episode_audio_task` in
  `app/activitypub/util.py`) documents Castopod's own shapes: a `Note` announcement whose link leads to a
  `PodcastEpisode` object with `audio: {url: {href, mediaType}}`.

## 3. Data model

### 3.1 `Community.kind`

(Amended: see §10 A3.)

- New column `kind = String(20)`, not null, server default `'discussion'`.
- Values: `discussion`, `microblogs`, `video_channel`, `podcast`, held in constants in `app/constants.py`.
- The migration sets `kind='microblogs'` on the row `find_microblogging_community` matches
  (`instance_id=1, user_id=1, name='microblogs'`). Downgrade drops the column.
- `find_microblogging_community` creates new rows with `kind='microblogs'`.
- `is_local_microblogs(community)` becomes `community.kind == 'microblogs' and community.is_local()`.
  `Post.is_microblog()` becomes `self.microblog and self.community.kind == 'microblogs'`. Every other
  name test for `'microblogs'` (`app/utils.py` SQL, `app/main/routes.py`, `app/relays/expiry.py`,
  `app/community/routes.py`, `app/community/live.py`) is changed to test `kind`. The plan lists each site.
- Remote communities keep `kind='discussion'`. This includes the podcast twin communities from
  `app/discovery/podcast.py`, so inbound behaviour does not change. `kind` changes only outbound
  behaviour, and only for local communities.

### 3.2 Who sets `kind`

- On create and edit of a local community, a moderator may choose `discussion`, `video_channel` or `podcast`.
- On edit, the choice is offered and accepted only while the community has zero posts, deleted ones included.
  Once it has posts, the form shows the kind read-only, and a POST that tries to change it is rejected with a
  form error.
- `microblogs` is never offered in a form. Only `find_microblogging_community` sets it.
- The alpha API's community create/edit takes the same field, with the same rules.

### 3.3 Podcast show fields

- New column `Community.podcast_category = String(64)`, nullable. It holds an Apple Podcasts category
  string; the form offers the fixed list.
- The show's cover is the community icon, and its language is the community's existing language.
  The show's author is the community's owner.

### 3.4 Per-post media metadata

Uploaded media metadata lives in `Post.extensions['media']`, following the fork's `extensions` convention:

```json
{"status": "processing|ready|failed", "error": "<reason, failed only>",
 "uuid": "<uuid4, set at upload>", "mime": "video/mp4", "bytes": 12345678,
 "duration_s": 312, "width": 1280, "height": 720, "fps": 30,
 "codec": "h264", "thumbnail_file_id": 987}
```

- `uuid` is set once at upload and never changes. PeerTube requires a stable `uuid` on every `Video`.
- `thumbnail_file_id` names an ordinary `File` row, so the existing image delete and S3 code paths cover it.
- Audio posts have no `width`, `height` or `fps`. `thumbnail_file_id` is set only when the file has
  embedded cover art.

Episode fields live in `Post.extensions['podcast']`, next to the existing `credits` key:
`episode_number` (int, optional), `season` (int, optional), `explicit` (bool, default false).

### 3.5 Settings

| Setting | Default | Meaning |
|---|---|---|
| `allow_video_file_uploads` | `no` (existing) | Master switch for video uploads |
| `allow_audio_file_uploads` | `no` | Master switch for audio uploads; same values as the video switch |
| `max_video_upload_bytes` | 524288000 (500 MB) | Per-file video limit |
| `max_audio_upload_bytes` | 209715200 (200 MB) | Per-file audio limit |

All four are on the admin misc settings form.

## 4. Serializers (`app/activitypub/serializers/`)

### 4.1 Dispatcher

(Amended: see §10 A1.)

`serialize_post(post, edit=False) -> dict` returns the AP object for a post. `send_post` wraps that object
in `Create` or `Update`. `post_to_page` returns it on fetch. Push and fetch therefore come from the same
function.

| Community `kind` | Post condition | Builder |
|---|---|---|
| `microblogs` | any type except poll and event | `microblog_note` |
| `video_channel` | `extensions.media.status == 'ready'` and mime `video/*` | `peertube_video` |
| `podcast` | `extensions.media.status == 'ready'` and mime `audio/*` | `podcast_episode` |
| anything else, any poll, any event | | `page` |

`page` is the current `Page` / `Question` / `Event` logic moved out of `send_post` and `post_to_page`
without behaviour changes. Golden tests prove its output is byte-identical to today's output before
and after the move.

Posts in `video_channel` or `podcast` communities that have no ready media (text posts from moderators,
link posts) use `page`.

### 4.2 `microblog_note`

(Amended: see §10 A2.)

(Amended: see §10 A7, A8, A12.)

- `type: Note`. `content` is the body HTML only, with no title. `contentMap` is set when the post has a language.
- Public posts: `to: [as:Public]`, `cc: [author followers collection URL, mentioned actors]`.
- Followers-only posts: the existing `followers_only_addressing` (Amendment B), unchanged.
- No `name`, no `audience`, and no community in `to` or `cc`.
- `attachment`: images as `Document` with `mediaType`, `url`, `name` (alt text), `width` and `height`
  when known.
- `sensitive: post.nsfw or post.nsfl`. `summary` carries `post.content_warning` when it is set.
- `tag`: `Hashtag` and `Mention` entries, as `tags_for_activitypub` builds them today.
- `url`: the post's HTML page. `published`, and `updated` on edit.
- `interactionPolicy.canQuote` stays as it is today.

### 4.3 `peertube_video`

(Amended: see §10 A11.) Confirmed against PeerTube v8.3.1 by the spike (§9 step 2). Evidence: §11. "Required"
means PeerTube rejects the object, crashes, or stores a video that cannot be listed or played without it.

Required:

- `type: Video`. `id`: the post's AP id. When a `Create` or `Announce` names a video PeerTube does not
  hold yet, PeerTube fetches the object from this id and uses the fetched copy. The id must therefore serve this
  same `Video` (push equals fetch) as `application/activity+json`. The id must be
  on the community's host: the channel is looked up by host.
- `uuid`: the media `uuid` (§3.4). It must be a version 4 UUID; a version 7 UUID is rejected.
- `name`: `post.title`. PeerTube rejects names shorter than 3 or longer than 120 characters, so the builder
  truncates longer titles to 120.
- `duration: "PT{duration_s}S"`, a string of whole seconds. PeerTube rejects an integer. It reads only the leading
  digits, so `PT1M5S` would become 1 second.
- `published` and `updated`, both always, not only on edit. Either one missing rejects the object.
- `views: 0`. A missing count rejects the object.
- `likes` and `dislikes`: the post's existing count collections, `/post/<id>/likes` and `/post/<id>/dislikes`.
  Without them PeerTube v8.3.1 stores the video and then crashes its whole process.
- `url`: a list holding a `Link` with `mediaType` set to the file mime (`video/mp4`, `video/webm` or `video/ogg`),
  `href` set to the file URL, `height` and `size`, plus `width` and `fps` when known. A plain mp4 `Link` plays in
  PeerTube's player: no HLS playlist, torrent or `streamingPlaylists` entry is needed. Without `height` or `size`,
  PeerTube drops the `Link` and keeps a video with no file, which cannot play. The file name in `href` must be
  unique among all videos the PeerTube instance knows, because PeerTube stores the remote file under that name.
  `<uuid>-<height>.<ext>` meets this.
- `icon`: `[{type: Image, url: <thumbnail URL>, mediaType, width, height}]`. A missing icon, or one without
  `width` and `height`, rejects the object.
- `attributedTo: [{type: Person, id: <author>}, {type: Group, id: <community>}]`. Only the `Group` is required;
  without it the video is rejected. The order and the string or object form do not matter. PeerTube credits the
  video to the community owner (§6.2), not to the `Person` named here.
- `to: [as:Public]` written in full as `https://www.w3.org/ns/activitystreams#Public`. Without it the video is
  stored as unlisted and is not listed under the channel. `cc: [community followers URL]`.
- `commentsPolicy: 1` when `post.comments_enabled`, else `2`. PeerTube v8 ignores `commentsEnabled`. A missing
  `commentsPolicy` means comments are disabled.

Optional (checked, accepted, no effect on listing or playback):

- `content`: the description as markdown, with `mediaType: text/markdown`. `text/html` is also accepted. If
  `content` is present it must be at least 3 characters and `mediaType` must be one of these two; otherwise the
  object is rejected. Without `content` the video has no description.
- A `Link` with `mediaType: text/html` and `href` set to the post page.
- `sensitive` (sets PeerTube's NSFW flag), `downloadEnabled`, `isLiveBroadcast: false`, `waitTranscoding: false`
  and `state: 1`. PeerTube defaults each of these when missing (`state` defaults to published).
- `support` (stored and shown as the support text), `language: {identifier, name}` and `tag` (`Hashtag` entries
  of 2 to 30 characters).

Media files must be served with `Access-Control-Allow-Origin` (for example `*`). PeerTube's player loads them
with `crossorigin="anonymous"`, and without the header the browser blocks playback. Flask-served static files
already send it (`app/request_hooks.py`); a reverse proxy or S3 bucket that serves media directly must add it.

### 4.4 `podcast_episode`

This mirrors what Castopod sends us. The Castopod spike (§9 step 5) confirms the shape or replaces it.

- The fetchable episode object: `type: PodcastEpisode`, `id: <episode URL>`, `name: post.title`,
  `content`: the description HTML,
  `audio: {type: Audio, url: {type: Link, href: <file URL>, mediaType: <mime>}, duration, size}`,
  `image`: the thumbnail, or else the community icon, `episodeNumber`, `seasonNumber`, `explicit`,
  `published`, and `attributedTo: <community>`.
- The activity sent out: `Create(Note)` with the community as actor. The Note `content` links the episode
  URL, the same way Castopod's own announcement Notes do, so `castopod_episode_url` would recognise it.
- `<episode URL>` is a new route, `/c/<community>/episodes/<post_id>`. It content-negotiates: AP clients get
  `PodcastEpisode`, browsers are redirected to the post page.

### 4.5 Podcast RSS

Communities of kind `podcast` serve a Podcasting 2.0 feed at their existing community RSS URL. Each episode
gets `<enclosure url length type>`, `<itunes:duration>`, `<itunes:episode>`, `<itunes:season>`,
`<itunes:explicit>` and `<podcast:guid>` (the media `uuid`). The channel gets `<itunes:category>`,
`<itunes:image>` and `<podcast:locked>no</podcast:locked>`. RSS for other kinds does not change.

### 4.6 Edits and deletes

`Update` carries the full re-serialized object from the same builder. `Delete` uses the existing
Tombstone path unchanged.

## 5. Media pipeline

### 5.1 Upload

- The upload field appears only when both conditions hold:
  - the user moderates the `video_channel` or `podcast` community;
  - the admin switch for that media type admits the user. `allow_audio_file_uploads` takes the same values as
    `allow_video_file_uploads` (`no`, `user 1`, `admins`, `users`). `can_upload_video` already decides video;
    a new `can_upload_audio` decides audio with the same logic, through one shared helper.
  The server repeats every one of these checks; hiding the field is not the control.
- The upload streams to a temp file in 1 MiB chunks while counting bytes. If the count passes the limit,
  the request is aborted with 413 and the temp file is removed. The file is never fully written.
- Flask `MAX_CONTENT_LENGTH` is set to `max(max_video_upload_bytes, max_audio_upload_bytes) + 10 MiB` as a
  global backstop. `deploy/DEPLOY_GUIDE.md` documents the matching reverse-proxy body limit.
- Type is checked by magic bytes, not by extension or client mime:
  - video: MP4/MOV (`ftyp`), WebM (EBML header);
  - audio: MP3 (ID3 tag or frame sync), M4A (`ftyp` M4A brand), Ogg (`OggS`, covering Vorbis and Opus).
  Anything else is rejected with a form error.
- The post is created with `extensions.media = {status: 'processing', uuid, mime, bytes}`, then
  `probe_media(post_id)` is queued.

### 5.2 `probe_media(post_id)` (celery)

1. Run `ffprobe -v error -print_format json -show_format -show_streams -protocol_whitelist file <path>`
   with a 60 s timeout, under `prlimit` with a 512 MiB address-space cap, and no network protocols.
2. Validate the result. Video needs a `codec_name` in {h264, vp9, av1}, a positive duration and positive
   dimensions. Audio needs a `codec_name` in {aac, mp3, opus, vorbis} and a positive duration. Any failure,
   timeout, non-zero exit or unparseable JSON sets `status='failed'` with a reason.
3. Video thumbnail: `ffmpeg -ss <duration*0.1> -i <path> -frames:v 1` (same whitelist and limits) produces
   an image, which goes through the existing image pipeline (`MEDIA_IMAGE_FORMAT`) into a `File` row.
   Audio thumbnail: embedded cover art is extracted the same way if there is a picture stream; otherwise
   the post has no thumbnail.
4. Write the metadata (§3.4) and set `status='ready'`, then queue `send_post`. A failure only sets
   `status='failed'` and notifies the author with the reason.

For remote media (S3), `probe_media` works on the local temp copy before upload to S3. The task stores to
S3 after probing, using the existing `store_files_in_s3` path.

### 5.3 Visibility and federation gating

- While `status != 'ready'` the post is listed and viewable only to its author, the community's moderators
  and admins. It does not federate, and `post_to_page` returns 404 for it.
- `send_post` checks `status == 'ready'` for any post with `extensions.media` and returns without sending
  otherwise.

### 5.4 Serving

Media is served by the existing static or S3 paths. Both must answer `Range` requests with `206`; the
harness asserts this for both a video and an audio file.

### 5.5 Deletion

The existing post-delete paths remove the media file and the thumbnail `File`, both locally and in S3.
Tests assert no file remains, following the static-tree leak gate (`tests/static_leak_gate.py`).

### 5.6 Dependency

`ffmpeg` (which provides `ffprobe`) is added to the container image and to the dev setup docs.

## 6. Delivery, actors and inbound

### 6.1 Delivery

(Amended: see §10 A6.)

(Amended: see §10 A9.)

Recipients are filtered as they are today: `can_deliver_to`, instance bans, user blocks, and
`local_only`/`private` communities never federate.

| Kind | Activity | Signed by | Recipients |
|---|---|---|---|
| `microblogs` | `Create(Note)` / `Update(Note)` | author | shared inboxes of the author's followers' instances and mentioned actors' instances. Threadiverse instances following `/c/microblogs` keep today's community `Announce`. |
| `video_channel` | `Create(Video)` / `Update(Video)`; `Delete` of a video post | author for `Create` and `Update`; the community owner for `Delete` | the shared inboxes of the community's follower instances. `MICROBLOG_APPS` get `Announce(<id>)` as today. |
| `podcast` | `Create(Note)` announcement | community | the community's follower instances |

(Amended: see §10 A11.) For `video_channel`, PeerTube v8.3.1 lists the video under the subscribed channel for
each of these deliveries: `Create(Video)` signed by the author, `Create(Video)` signed by the community, or
`Create` by the author followed by `Announce(<id>)` by the community. One `Create(Video)` signed by the author is
enough, so the community sends no `Announce` to PeerTube. An `Announce` by the community is accepted and recorded
as a share. An `Announce` that wraps the `Create`, which is the form threadiverse followers get today, is ignored
by PeerTube. For a video it does not hold yet, PeerTube fetches the `Video` from its `id` on `Create` and
`Announce` and uses the fetched copy. An `Update` is applied from the embedded object without a fetch. An `Update` is accepted from any actor on the community's host.
A `Delete` of a video is accepted only from the community owner, the `Person` in the community's `attributedTo`
(§6.2). PeerTube rejects a `Delete` signed by the author, unless the author is the owner, and one signed by the
community.

### 6.2 Actors

- `video_channel` and `podcast` communities keep their `Group` actor, keys, inbox and follower handling.
- The spikes list any actor properties PeerTube or Castopod require. Only properties a spike proves required
  are added, and only to the community kinds that need them.
- (Amended: see §10 A11.) A `video_channel` community's `Group` sets `attributedTo: [{type: Person, id: <owner>}]`,
  where the owner is the community's creator, `Community.user_id`. A PeerTube channel has exactly one account, and
  the `Person` must be on the community's host. PeerTube refuses to subscribe to a `Group` without such a
  `Person`; today's `attributedTo`, the moderators collection URL, fails.
  PeerTube makes this `Person` the channel's account: every video in the channel is shown under it, and only it
  may delete them (§6.1). `moderators` keeps the moderators collection URL. PieFed reads moderators from
  `moderators` when `attributedTo` is not a string. Lemmy 0.19 parses this list as its PeerTube form and then
  has no moderators URL.
- (Amended: see §10 A11, A12.) The `Accept` (or `Reject`) of a `Follow` of a `video_channel` community goes to the
  follower's own `inbox`, not to its shared inbox. PeerTube ignores an `Accept` that arrives at its shared inbox,
  so the subscription stays pending. Today PieFed stores only the shared inbox of a remote user and sends the
  `Accept` there.
- No new actor types, inboxes or URL namespaces, apart from the episode route in §4.4.

### 6.3 Following

- A PeerTube subscription or a Castopod follow sends `Follow` to the Group, which the existing community
  follow handling accepts.
- A Mastodon follow of a local user is handled by the existing `User` follow handling.

### 6.4 Inbound interactions

(Amended: see §10 A10, A12.)

These go through the existing handlers, keyed by our object id:

- `Note` with `inReplyTo` set to one of our objects becomes a comment. This covers Mastodon replies,
  PeerTube comments and Castopod replies.
- `Like` becomes an upvote, and `Dislike` (PeerTube) a downvote.
- `Announce` of our object counts as a boost.
- PeerTube `View` and `CacheFile` stay ignored.
- (Amended: see §10 A11.) PeerTube reuses its rating ids: a `Like` is always
  `<account>/likes/<PeerTube video id>`, and the `Undo` of it is that id plus `/undo`. After a like, an unlike and
  a second like, PieFed drops the second `Like` as a duplicate activity id, so the upvote is lost. `Dislike` ids
  behave the same way.

Any gap found by the harness is fixed in the inbound handler, test-first.

## 7. Security

(Amended: see §10 A5.)

- Uploads: server-side role and switch checks, streamed size limit, magic-byte type check, and a global
  `MAX_CONTENT_LENGTH`.
- ffmpeg/ffprobe run only on local files under `-protocol_whitelist file`, with timeout and memory caps,
  so an uploaded playlist cannot make the server fetch URLs (SSRF) and a malformed file cannot exhaust
  the worker.
- Nothing about a post is federated before probing succeeds, so remote servers never receive objects
  with missing or false metadata.
- New routes (§4.4) apply the same visibility checks as `post_to_page`: private, local-only, deleted and
  not-ready posts return 404.

## 8. Testing

### 8.1 Interop harness

(Amended: see §10 A4.)

- `compose.interop.yaml` plus `tests/interop/`, run with `podman compose`.
- Services: PieFed (web, celery, postgres, redis) built from the working tree, Mastodon, PeerTube and
  Castopod, each pinned by image digest. Caddy issues certificates from a local CA for `piefed.test`,
  `mastodon.test`, `peertube.test` and `castopod.test` on one network, and every container trusts that CA.
- Setup seeds accounts and API tokens on every app.
- Tests are marked `interop` and are excluded from the default pytest run. `tests/README.md` documents
  the command.
- Each test makes a real post on PieFed, asserts native display through the target app's own API, then
  replies, likes or boosts from the target and asserts the interaction arrives in the PieFed thread.
- Polling is bounded with short timeouts. A slow interop test is a bug to fix, not a timeout to raise.

### 8.2 Unit tests (TDD)

- Golden tests for each builder, checked against objects captured from real Mastodon, PeerTube and
  Castopod output, saved under `tests/fixtures/`.
- `page` golden tests run before and after the move out of `send_post` (§4.1).
- Push and fetch are equal for every kind and post type.
- The migration backfill and downgrade, and every `kind` read site.
- The `kind` change rules, both form and API.
- Upload: each limit at its edge, the 413 abort leaving no temp file, every magic-byte accept and reject
  case, and the role and switch checks.
- `probe_media`: sample files for each allowed codec, a rejected codec, a hostile playlist file that must
  be refused, timeout, non-zero exit, bad JSON, and audio with and without cover art.
- Gating: no send and a 404 fetch while not ready. Failure notifies the author.
- Deletion leaves no media or thumbnail file behind.
- Podcast RSS and the episode route's content negotiation and visibility.

### 8.3 Fuzz

Atheris targets for the three new builders and for `ffprobe` JSON validation, added to the fuzz track's
gate.

### 8.4 Coverage (R9)

- Every line and branch this work adds or changes under `app/` is executed by the unit suite:
  `tests/check_changed_line_coverage.py coverage.json <base> --branches` reports zero misses.
- Each new module (`app/activitypub/serializers/*`, the media probe module, the upload streaming module)
  gets a floor of 100 in `coverage_floors.ini`.
- Coverage counts the unit suite only. Interop tests run against other containers and are not counted,
  so they cannot stand in for unit tests.
- Lines excluded from coverage need a written reason in the code. They are allowed only for code that
  cannot run under test, and the reviewer checks each one.

## 9. Build order

Each step gets its own implementation plan and its own commits, and each ends with its gates green
(unit suite, the coverage checks of §8.4, and the interop tests for its target).

0. Harness with PieFed and Mastodon only.
1. Microblogs: `Community.kind`, `microblog_note`, and the `page` move. The harness proves Mastodon shows
   the status natively and that replies, likes and boosts come back.
2. PeerTube spike: add PeerTube to the harness; hand-craft `Video` objects from PieFed to find the minimal
   object and actor PeerTube accepts and shows under a subscribed channel. The code is throwaway. The
   findings replace §4.3, §6.1 and §6.2 for video, and the owner reviews the change.
3. Media pipeline (§5).
4. Video channels: `peertube_video` and the actor properties from the spike.
5. Castopod spike: add Castopod to the harness and find out whether it shows a non-Castopod podcast actor's
   episodes. If it does, the findings replace §4.4 and §6.1 for podcasts. If it does not, podcast scope
   becomes RSS (§4.5) plus `Audio` objects for Mastodon and PieFed, and the owner decides before step 6.
6. Podcasts: `podcast_episode`, the episode route, RSS, and Castopod delivery as decided after step 5.

## 10. Amendments (2026-10-14, plan for steps 0–1)

- **A1 (§4.1):** there is no separate `serialize_post`.
  - `post_to_page` stays the single fetch-side entry point and returns `microblog_note(...)` for microblog posts.
  - `send_post` branches to `send_microblog_note`.
  - The existing `Page`/`Question`/`Event` code stays where it is and is not moved. Its push and fetch versions already differ (D298–D300), and fixing that is outside this feature.
  - "Push equals fetch" applies to the new builders.
- **A2 (§4.2):** local microblog posts carry a title the user typed, because the form requires one. The Note `content` starts with the title as its own paragraph, except when the body's text already starts with the title. A link or video-link post's title paragraph links to `post.url`. The duplication the spec feared happens only when the title was derived from the body, which this test catches.
- **A3 (§3.1):** `Community.kind` lands in step 4, where it is first used. Step 1 keys on the existing `is_local_microblogs(community)`.
- **A4 (§8.1):** the harness tests live in a top-level `interop/` directory, not `tests/interop/`. `tests/conftest.py` blocks all outbound HTTP for everything under `tests/`, and the interop tests need real HTTP.
- **A5 (§7):** new env `ALLOWED_PRIVATE_NETWORKS`, a comma-separated list of CIDRs, empty by default. `is_refused_address` admits addresses inside these networks. Only the harness sets it, so containers can reach each other on the podman network. Production leaves it unset.
- **A6 (§6.1):** for microblogs, the author's `Create`/`Update` goes to every follower and mention instance, whether or not that instance also gets the community's `Announce`. Receivers dedupe by object id. Today's cross-dedupe can leave a Mastodon follower with only a boost.
- **A7 (§4.2):** `sensitive` is true also when `content_warning` is set. This matches `post_to_page` and Mastodon's CW semantics.
- **A8 (§4.2), replaced by A12:** body `#words` in a microblogs post become `Hashtag` tags and are linked in `content`. This is serialization-only; nothing is stored. The rule is the `HASHTAG_RE` regex in `app/activitypub/serializers/microblog.py` (a `#` at the start or after a non-alphanumeric, then a Unicode word that starts with a letter or digit and holds at least one letter, so `#1` is no hashtag), applied to text nodes outside `a`, `code` and `pre`, and deduped against explicit tags.
- **A9 (§6.1):** the group `Announce` copy sent to threadiverse community followers carries `audience` (the community) on both its inner activity and the inner object, so Lemmy and mbin can file the post. The author's `Create`/`Update`, the microblog `Announce` of the id, and the fetched object carry none.
- **A10 (§6.4):** an `Announce` of a local post is accepted from an accepted inward follower of its author, unless the author blocked the announcer or the announcer's instance, the post is deleted, or the announcer cannot view it (`can_view`). A followers-only post is therefore never boosted by a remote follower: `can_view` reads outward follows only.
- **A11 (§4.3, §6.1, §6.2, §6.4), PeerTube spike (step 2), approved by the owner 2026-10-15, including its rulings (owner `Person` is `Community.user_id`, titles cut to 120 characters, author-signed `Create(Video)` with no `Announce`, owner-signed `Delete`):** PeerTube v8.3.1 shows a PieFed
  community's uploaded video as a native, playable video under the subscribed channel, given four changes.
  - The `Group` of a `video_channel` community names its owner `Person` in `attributedTo` (§6.2). Without it
    PeerTube cannot subscribe.
  - The `Accept` of a `Follow` goes to the follower's own inbox (§6.2). PeerTube ignores one at its shared inbox.
  - The `Video` carries the fields §4.3 now lists as required. The draft lacked `views`, `likes`, `dislikes`,
    `commentsPolicy` and an always-present `updated`. Missing `likes` and `dislikes` crash PeerTube. Media is
    served with `Access-Control-Allow-Origin`.
  - Delivery is one `Create(Video)` / `Update(Video)` signed by the author. A `Delete` is signed by the community
    owner, because PeerTube accepts a video `Delete` only from the channel's account (§6.1).
  - Inbound comments, likes and dislikes already work, and `View` is ignored. PeerTube's reused rating ids make
    PieFed drop a repeated like as a duplicate (§6.4).
  - Evidence: §11.
- **A12 (§4.2, §6.2, §6.4), owner rulings 2026-10-15:**
  - **Boosts (§6.4, amends A10):** an `Announce` of a post this instance holds is recorded only when the post is
    public or unlisted (`is_open`). A followers-only post is never boosted, whoever the announcer is. `can_view` is
    not the test: it admits the post's author and the users the post Mentions. Refused announces log
    `Boosted post is not public`; a deleted post logs `Boosted post is deleted`.
  - **Follow answers (§6.2, widens A11):** the `Accept` or `Reject` of every `Follow` goes to the follower
    actor's own `inbox`: follows of a community, a feed or a local user, answered on receipt or by hand. A remote
    user's own inbox is stored in `user.ap_own_inbox_url`, beside the shared inbox in `ap_inbox_url`, under the
    same own-host rule. It is stored when the actor is created, and for an actor stored earlier on its next
    refresh; there is no bulk backfill. Until then the answer goes to `ap_inbox_url`. Everything else is still
    delivered to the shared inbox. Mastodon and Lemmy accept either inbox; PeerTube only the actor's own.
    `Community.kind` (§3.1) lands with this change, because the §6.2 `attributedTo` rule of a `video_channel`
    community is what lets PeerTube subscribe at all. Only the column and its constants are added: no form sets
    the kind yet, and the microblogs row is not marked (step 4).
  - **Hashtags (§4.2, replaces A8):** when a local post in the microblogs community is created or edited, each
    body `#word` is stored in `post.tags`, beside the explicit tags and once per name. The words are the ones
    A8's `HASHTAG_RE` rule links in `content` (`link_hashtags`). An edit recomputes them, so a word taken out of
    the body drops its tag unless it is also an explicit tag. The Note's `tag` list is built from `post.tags`
    only; nothing is derived from the body at serialization. Content linking is unchanged. Posts written before
    A12 are not backfilled.
  - **Fix round 1 (2026-10-15):**
    - A boosted post fetched because we did not hold it is stored with the visibility its audience gives, and the
      boost is refused unless that is public or unlisted, as for a held post.
    - A body `#word` longer than 256 characters (`Tag.name` and `display_as` are `String(256)`) is linked in
      `content` but not stored as a tag.
    - The web edit form, and the crosspost form filled from a source post, fill the tags field of a local
      microblogs post with `post.tags` minus its body hashtags. A tag that was both explicit and in the body
      becomes body-only.
    - A new Tag made from a body hashtag shows the spelling first used (`#SolarStorm`); matching stays case-blind.
    - When a follower stored before `ap_own_inbox_url` existed follows again, its actor document is fetched once,
      synchronously, before the answer. The document must name the follower and pass the own-host rule of
      `delivery_inbox_of`. Its `inbox` is then stored and used. If the fetch fails, the answer goes to the shared
      inbox.

## 11. Evidence: PeerTube spike (2026-10-15)

PeerTube `docker.io/chocobozzz/peertube:v8.3.1-trixie@sha256:1f161d08b113d77410b8051f764d16a05267692f3b0a3a7b2f0cf887f80ed082`
(source tag `v8.3.1`, commit `3324876`), run in the interop harness (`compose.interop.yaml`). Hand-made objects were
served from `piefed.test` and signed with PieFed's own `send_post_request` (`app/activitypub/signature.py`). A
PeerTube user subscribed to probe communities. Captures are in `tests/fixtures/peertube_*_v8.json`. Source citations
are paths in the PeerTube repository at `v8.3.1`.

### 11.1 Subscription

| Probe | Result |
|---|---|
| Subscribe to `microblogs@piefed.test` and `vc1@piefed.test`, actor as today | Refused: `Cannot find account attributed to video channel https://piefed.test/c/vc1` |
| `attributedTo: [{type: Person, id: <owner>}]` | `Follow` sent; PieFed records the member and sends `Accept` to `https://peertube.test/inbox` |
| `attributedTo: "<owner URL>"` | Same as the row above |
| `attributedTo: [<moderators URL>, <owner URL>]` | Same as the row above |
| `Accept` at PeerTube's shared inbox (PieFed today) | Ignored: `Need to accept on explicit inbox.`; subscription stays `pending` |
| The same `Accept`, signed by the community and posted to `https://peertube.test/accounts/carol/inbox` | Subscription `accepted`; the channel shows `ownerAccount` alice |

Source: `server/core/lib/activitypub/actors/get.ts:59-60` and `:87-91` (a `Group` needs an owner),
`:105-134` (`findOwner`: same host, `type: Person`); `server/core/helpers/custom-validators/activitypub/actor.ts:83-85`;
`server/core/lib/activitypub/process/process-accept.ts:9` and `process-reject.ts:9` (explicit inbox only). PieFed:
`app/activitypub/util.py:2175` stores the shared inbox as `ap_inbox_url`; `app/activitypub/routes.py:1295` sends the
`Accept` there. Lemmy 0.19.13 `crates/apub/src/protocol/objects/mod.rs:132-151` parses a list of `{type, id}` as
`AttributedTo::Peertube`, which yields no moderators URL.

### 11.2 Delivery

All variants used one subscribed community and the shared inbox `https://peertube.test/inbox`.

| Variant | Listed under the channel | Fetch |
|---|---|---|
| (a) `Create(Video)` signed by the author | yes | re-fetched from its `id` on `piefed.test` |
| (b) `Create(Video)` signed by the community | yes | re-fetched from its `id` |
| (c) (a), then `Announce(<id>)` by the community | yes; the `Announce` is stored as a share | re-fetched from its `id` |
| `Announce(<id>)` by the community only | yes | re-fetched from its `id` |
| `Announce(Create(Video))` by the community (today's threadiverse form) | no | PeerTube fetched the `Create` id as a video and gave up |
| (a) by a non-owner author | yes, shown under the owner's account | re-fetched from its `id` |
| `Update(Video)` by the owner, by the community, or by a non-owner author | applied in each case | not fetched; the embedded object is used |
| `Delete` by a non-owner author | refused: `Account https://piefed.test/u/dave does not own video channel` | |
| `Delete` by the community | refused: `Account https://piefed.test/c/vc2 does not own video channel` | |
| `Delete` by the owner | video removed | |

Source: `server/core/helpers/custom-validators/activitypub/activity.ts:109-121` (a `Create` must embed a valid
object); `server/core/lib/activitypub/videos/get.ts:78` and `videos/shared/url-to-object.ts:12-14` (fetch by id, same
host); `videos/shared/abstract-builder.ts:47-58` (channel from the `Group` in `attributedTo`, or `audience`);
`process/process-announce.ts:22` (only a `Group` or `Application` announce counts) and `:44`;
`process/process-update.ts:90-93` and `:100-104`; `process/process-delete.ts:92-94`. PeerTube itself signs a video's
`Create` and `Delete` with the channel's account (`send/send-create.ts:43`, `send/send-delete.ts:33`) and
`Announce`s it from the channel (`share.ts:102`).

### 11.3 The Video object

Base object: the §4.3 required fields. Each probe changed one field and was delivered as (a). The validator is
`sanitizeAndCheckVideoTorrentObject` in `server/core/helpers/custom-validators/activitypub/videos.ts:44-108`. Its
debug log names the failing field, quoted below.

| Probe | Result | Source |
|---|---|---|
| no `uuid`; a version 7 UUID | rejected: `uuid` | `videos.ts:88`, `misc.ts` (`custom-validators`) `:65-67` |
| no `duration`; `duration: 10` | rejected: `duration format` | `activitypub/misc.ts:91-97`, `videos.ts:82-83` |
| `PT1M5S` | not probed; parsed as 1 | `server/core/lib/activitypub/activity.ts:17-18` |
| no `icon`; `icon` without `width`/`height` | rejected: `icons` | `videos.ts:57`, `activitypub/misc.ts:65-89` |
| `icon` as one object, not a list | accepted | `activitypub/misc.ts:66` |
| no `views` | rejected: `views` | `videos.ts:93` |
| no `updated`; no `published` | rejected: `updated`; `published` | `videos.ts:94-95` |
| no `likes` (with `dislikes`); neither | accepted, stored, then the PeerTube process exits: `Named replacement ":count" has no entry in the replacement map` | `videos/shared/video-sync-attributes.ts:31`, `:56-72`; `server/core/models/video/video.ts:1624-1635`; `server/core/helpers/logger.ts:196-199` |
| `likes`/`dislikes` set to PieFed's `/post/<id>/likes` and `/dislikes` | accepted; PeerTube synced `likes = 1` | `app/activitypub/routes.py:2859` |
| no `content` and no `mediaType` | accepted, no description | `videos.ts:99` |
| `content` without `mediaType`; `content: "ab"` | rejected: `mediaType/content` | `videos.ts:99`, `:222-224`; `server/core/initializers/constants.ts:497` |
| `mediaType: text/html` | accepted | `videos.ts:223` |
| no `state`; no `downloadEnabled`, `isLiveBroadcast`, `waitTranscoding`, `sensitive` | accepted with defaults | `videos.ts:62-68` |
| `sensitive: true` | accepted, `nsfw = true` | |
| no `commentsEnabled` and no `commentsPolicy`; `commentsEnabled: true` | accepted, comments disabled | `videos.ts:71-77`; policy enum `packages/models/src/videos/comment/video-comment-policy.enum.ts:1-5` |
| `commentsPolicy: 1` | accepted, comments enabled | |
| `support` | accepted and stored | |
| `name` of 121 characters; of 2 characters | rejected: `name` | `videos.ts:80`, `constants.ts:494` |
| only the mp4 `Link` (no `text/html` `Link`) | accepted, 1 file | |
| mp4 `Link` without `height`; without `size` | accepted with 0 files | `videos.ts:110-119`, `:226-249` (bad links are dropped, not rejected) |
| mp4 `Link` without `fps`; `mediaType: video/webm` | accepted, 1 file | `videos.ts:118`, `constants.ts:907-913` |
| only the `text/html` `Link` | accepted with 0 files | |
| a second video using the same file name | refused: `filename must be unique` | `videos/shared/object-to-model-attributes.ts:100`, `server/core/models/video/video-file.ts:116-119` |
| `attributedTo` `[Group, Person]`; `[Group]` only; plain URL strings | accepted | `activitypub/misc.ts:56-63` |
| `attributedTo: [Person]` only | refused: `Cannot find associated video channel` | `abstract-builder.ts:55` |
| `attributedTo: [Person]` plus `audience: <Group>` | accepted | `actors/get.ts:115` |
| no `to` and no `cc` | accepted as unlisted (privacy 2), not listed under the channel | `videos/shared/object-to-model-attributes.ts:270-273` |
| `tag` with `Hashtag`s; `language` | accepted, stored | `videos.ts:169-184` |

Playback: `GET /api/v1/videos/{uuid}` for the base object showed `duration: 10`, one 360p file with `fileUrl` on
`piefed.test`, no streaming playlists, and a thumbnail that PeerTube served from `/lazy-static/thumbnails/`
(200, `image/jpeg`). From the PeerTube container the file answered a `Range` request with 206, and `ffprobe`
read h264 640x360 and aac, 10 s. Headless Chromium (Playwright 1.63.0), logged in as the PeerTube user, opened
`/w/{uuid}`. Without `Access-Control-Allow-Origin` on the file, the browser blocked it and the player failed with
`MEDIA_ERR_SRC_NOT_SUPPORTED`. With the header, the video played to `currentTime 9.47` of `duration 10`, at
640x360. The player sets `crossorigin="anonymous"` (`client/src/standalone/player/src/shared/web-video/web-video-plugin.ts:32`)
and uses web-video mode when there is no streaming playlist (`client/src/standalone/videos/shared/player-options-builder.ts:175-176`).
Anonymous visitors get 403 for remote videos of a host the instance does not follow
(`server/core/middlewares/validators/videos/videos.ts:298-305`); this is PeerTube's default and applies to any
remote video.

Production check (2026-10-15): `https://hell.cloud/static/media/posts/...` is served by gunicorn behind Caddy
(`server: gunicorn`, `via: 1.1 Caddy`), not by S3 or a Caddy file server. It answers with
`access-control-allow-origin: *` and `accept-ranges: bytes`, and a `Range` request gets 206. In Chromium
(Playwright MCP) on `https://example.com`, a `mode: 'cors'` fetch with `Range: bytes=0-1023` returned 206 and
1024 bytes, and an image loaded with `crossOrigin = 'anonymous'` drew to a canvas that stayed readable. The
header comes from the global `after_request` hook (`app/request_hooks.py:149`), so it covers every Flask-served
media type, including video. hell.cloud has no local video yet, so a video file was not loaded.

### 11.4 Inbound

A real PieFed post served a `Video` at its own id. The PeerTube user then acted on it.

| PeerTube action | Activity PieFed received | PieFed today |
|---|---|---|
| subscribe | `Follow` (`peertube_follow_v8.json`) | accepted; `Accept` sent to the shared inbox (11.1) |
| comment | `Create(Note)`, `inReplyTo` the post id (`peertube_comment_v8.json`) | accepted; a reply in the post's thread |
| like | `Like` (`peertube_like_v8.json`) | accepted; upvote |
| dislike | `Undo(Like)`, then `Dislike` (`peertube_undo_like_v8.json`, `peertube_dislike_v8.json`) | accepted; the upvote becomes a downvote |
| remove rating | `Undo(Dislike)` (`peertube_undo_dislike_v8.json`) | accepted; vote removed |
| like again after an undo | `Like` with the same id as before | ignored: `Already aware of this activity`; no vote |
| view | `View` from the instance actor `https://peertube.test/accounts/peertube` (`peertube_view_v8.json`) | ignored: `PeerTube View or CacheFile activity` |

### 11.5 Golden captures

`tests/fixtures/peertube_video_v8.json` is a video uploaded to PeerTube itself and fetched from its id. It has an HLS
playlist and a web-video mp4, five icons, `attributedTo` naming the account and the channel, and `commentsPolicy`.
`tests/fixtures/peertube_channel_v8.json` is its channel. Its `attributedTo` is a list of URL strings naming the
account.

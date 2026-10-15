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

The starting shape below is confirmed or corrected by the PeerTube spike (§9 step 2), and the confirmed
shape replaces this section before step 4 begins.

- `type: Video`, `id: post.public_url()`, `uuid`, `name: post.title`.
- `duration: "PT{duration_s}S"`.
- `content`: the description as markdown, with `mediaType: text/markdown`.
- `url`: a list holding
  - a `Link` with `mediaType: text/html` and `href` set to the post page, and
  - a `Link` with `mediaType` set to the file mime, `href` set to the file URL, and `height`, `size` and `fps`.
- `icon`: `[{type: Image, url: <thumbnail URL>, mediaType, width, height}]`.
- `attributedTo: [{type: Person, id: <author>}, {type: Group, id: <community>}]`.
- `commentsEnabled: post.comments_enabled`, `downloadEnabled: true`, `sensitive`, `state: 1`,
  `isLiveBroadcast: false`, `waitTranscoding: false`, `published`, and `updated` on edit.
- Addressing: `to: [as:Public]`, `cc: [community followers URL]`.

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

Recipients are filtered as they are today: `can_deliver_to`, instance bans, user blocks, and
`local_only`/`private` communities never federate.

| Kind | Activity | Signed by | Recipients |
|---|---|---|---|
| `microblogs` | `Create(Note)` / `Update(Note)` | author | shared inboxes of the author's followers' instances and mentioned actors' instances. Threadiverse instances following `/c/microblogs` keep today's community `Announce`. |
| `video_channel` | `Create(Video)` / `Update(Video)` by the author, then `Announce` by the community | author, then community | the community's follower instances. `MICROBLOG_APPS` get `Announce(<id>)` as today. |
| `podcast` | `Create(Note)` announcement | community | the community's follower instances |

The PeerTube spike decides whether PeerTube needs the `Create(Video)` from the account, from the channel,
or as an `Announce` from the channel. This table changes to match before step 4.

### 6.2 Actors

- `video_channel` and `podcast` communities keep their `Group` actor, keys, inbox and follower handling.
- The spikes list any actor properties PeerTube or Castopod require, such as `attributedTo` naming the
  owner `Person` on a PeerTube channel. Only properties a spike proves required are added, and only to
  the community kinds that need them.
- No new actor types, inboxes or URL namespaces, apart from the episode route in §4.4.

### 6.3 Following

- A PeerTube subscription or a Castopod follow sends `Follow` to the Group, which the existing community
  follow handling accepts.
- A Mastodon follow of a local user is handled by the existing `User` follow handling.

### 6.4 Inbound interactions

These go through the existing handlers, keyed by our object id:

- `Note` with `inReplyTo` set to one of our objects becomes a comment. This covers Mastodon replies,
  PeerTube comments and Castopod replies.
- `Like` becomes an upvote, and `Dislike` (PeerTube) a downvote.
- `Announce` of our object counts as a boost.
- PeerTube `View` and `CacheFile` stay ignored.

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

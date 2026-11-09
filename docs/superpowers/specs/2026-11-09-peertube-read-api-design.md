# PeerTube read API: NewPipe on PieFed

Date: 2026-11-09
Status: owner rulings V1-V3 given 2026-11-09 (§6), D4 ruled 2026-11-07; part of the API-completeness plan
(`docs/superpowers/plans/2026-11-07-api-completeness.md`, workstream W2.1). Nothing here is built yet.

## 1. Goal

A NewPipe user adds a PieFed domain as a PeerTube instance. They browse its videos (trending, most liked, recent,
local), open one and play it, read its comments, open the uploader's account and the community's channel, and search.
Proved by the harness: NewPipe on the Android tier, driven for real; its matrix `view` row flips from `unsupported` to
`passed` (`login` and `post` stay inapplicable: NewPipe has no accounts and cannot upload).

Anonymous and read-only. Login, subscriptions and comments are W2.2; uploads are W2.3.

## 2. What NewPipe reads

From NewPipeExtractor's PeerTube service (`dev` branch, read 2026-11-09):

- **Adding an instance:** `GET /api/v1/config`, of which it reads only `instance.name`. No version check (the official
  PeerTube app's gate is D4, W2.2).
- **Lists:** every list is paged `start=N&count=12` and answers `{total, data[]}`. The next page is `start + 12` until it
  reaches `total`, so `total` must be right. An item is taken as a playlist if it has `videosLength`, as a channel if it
  has `followersCount`, otherwise as a video, so a video never carries either key.
- **Kiosks:** `/api/v1/videos?sort=-trending`, `-likes`, `-publishedAt`, and `-publishedAt&isLocal=true`.
- **A video:** `/api/v1/videos/{id}` (any string as the id). Required: `uuid`, `name`, `publishedAt`, `nsfw`,
  `account.{name,host,displayName}`, `channel.{url,displayName}`, `category.label`, `licence.label`, `privacy.id`.
  Optional: `description`, `duration`, `views`, `likes`, `dislikes`, `tags`, `language.id`, `support`, thumbnails
  (`thumbnailPath`, `previewPath`) and avatars (`avatars[]{path,width}`).
  - It also fetches `/description` when the description is exactly 250 characters ending in "...", plus `/captions`,
    `/chapters` and the storyboards.
  - Related videos come from `/api/v1/search/videos?tagsOneOf=` when the video has tags, otherwise from
    `/api/v1/accounts/{name}@{host}/videos`.
- **Playback:** `files[]` are progressive streams; `streamingPlaylists[]` (HLS) is optional. Per file:
  - `fileUrl` (or `fileDownloadUrl`) must be an absolute URL.
  - `resolution.label` is required. A label containing "audio" marks an audio stream.
  - `torrentUrl` must be present, even as an empty string; without it the whole list fails.
  - The format comes from the URL's extension (mp4, webm, m4a).
  - `hasAudio: false` marks a stream video-only.
- **Comments:**
  - `/api/v1/videos/{id}/comment-threads` lists the top-level comments. Each needs `text` (HTML), `createdAt`,
    `account.{name,host,displayName}` and `totalReplies`, plus `threadId` when it has replies. Items with `isDeleted`
    are skipped.
  - `/comment-threads/{threadId}` answers `{comment, children[]}`, where each child is again `{comment, children}`.
- **Accounts and channels:**
  - `/api/v1/accounts/{h}` and `/api/v1/video-channels/{h}`, with tabs `/videos`, `/video-channels` and
    `/video-playlists`.
  - A channel needs `displayName`, `ownerAccount.{name,url}`, `followersCount` and `description`. An account needs
    `displayName`.
- **Search:** `/api/v1/search/videos`, `/search/video-channels` and `/search/video-playlists`, each taking `search=q`.
- **Images, captions and storyboards** are paths, which NewPipe prefixes with the instance's base URL, so they must be
  served from the domain asked.

## 3. Mapping

A **video** is a published post of type video that the anonymous reader may read on the domain asked (the read layer,
`app/reads/posts.post_listing`, with the domain's own restrictions, as the Mastodon public timeline uses). It also
needs a playable file (V1):

- **An upload:** a local post whose media record is `ready`. Its file is on the post's domain, with height, duration,
  size and thumbnail known.
- **A remote file:** a post whose URL is itself a video file (.mp4, .webm). NewPipe streams it from the other server.
  These are left out on an onion domain, where a viewer must reach nothing but the onion.
- **Left out:** links to video sites (YouTube, a PeerTube or Loops watch page) and failed or processing uploads.

| PeerTube | PieFed |
|---|---|
| `id` | the post id |
| `uuid` | a UUID that holds the post id (`00000000-0000-4000-8000-` + 12 hex digits), so a lookup needs no column (an upload's media uuid would need a scan of every post's media record) |
| `name`, `description` | title; the body, in Markdown as PeerTube serves it. A list item's description is cut to 250 characters ending in "...", as PeerTube does, so NewPipe fetches `/description` for the rest |
| `publishedAt`, `updatedAt` | `posted_at`, `edited_at` |
| `duration` | the upload's `duration_s`; 0 for a remote file |
| `views` | 0 (PieFed counts none) |
| `likes`, `dislikes` | up and down votes |
| `nsfw` | nsfw or nsfl |
| `category`, `licence` | `{id: null, label: "Unknown"}`, as PeerTube sends for an unset one |
| `language` | the post's language, or `{id: null, label: "Unknown"}` |
| `privacy` | `{id: 1, label: "Public"}`: only what the anonymous reader may read is listed |
| `tags` | the post's hashtags |
| `account` | the author: `name`, `host`, `displayName`, `url`, `avatars` |
| `channel` | the community (V2): `name`, `host`, `displayName`, `url`, `avatars` |
| `thumbnailPath`, `previewPath` | the post image's thumbnail and image, as paths on the domain asked; left out when the file is not stored here |
| `files[]` | per §3.1 |
| `streamingPlaylists` | `[]` (no HLS) |
| `isLive` | false |

### 3.1 files[]

- **An upload:** one entry for the stored file, plus one per transcoded rendition (V3, phase 2). Each entry has:
  - `fileUrl` and `fileDownloadUrl` on the domain asked (`storefront_link`): the stored URL names the uploader's
    domain, and JSON gets no link rewrite, so an onion's viewer would otherwise be sent to the clearnet;
  - `resolution` `{id: height, label: "<height>p"}`, plus `size` and `fps`;
  - `torrentUrl: ""`, `magnetUri: ""`.
- **A remote file:** one entry. `fileUrl` is the post's URL, and `resolution` is `{id: 0, label: "Original"}`.

The largest entry comes first, as PeerTube orders them.

### 3.2 Comments

- A thread is a top-level reply, and `threadId` is its id.
- `/comment-threads/{id}` answers the reply's subtree, through the same reader gate as the web page. Deleted
  replies, and replies whose parent is not shown, are left out, as the Mastodon thread leaves them out.
- `text` is the reply's HTML, `totalReplies` counts its descendants, and `totalRepliesFromVideoAuthor` counts those
  written by the post's author.

### 3.3 Accounts and channels

- An **account** is a person: `/api/v1/accounts/{name}` or `{name}@{host}`.
  - `/videos` lists their videos.
  - `/video-channels` lists the communities they moderate.
  - `/video-playlists` is empty.
- A **channel** is a community, any community (V2): `/api/v1/video-channels/{name}` or `{name}@{host}`.
  - `/videos` lists its videos.
  - `followersCount` is its subscriber count.
  - `ownerAccount` is its first moderator, or its owner.
- A banned, deleted or private actor answers 404, as PeerTube does.

### 3.4 Other endpoints

- **`/api/v1/config`:**
  - `instance.name` is the domain's site name.
  - `serverVersion` is the PeerTube API level served (D4), not PieFed's version.
  - Any other keys NewPipe or the PeerTube app read at startup get PeerTube's defaults. Signup is off here, until W2.2.
- **`/api/v1/videos` sorts:**
  - `-trending` → PieFed's hot ranking;
  - `-likes` → score;
  - `-publishedAt` → newest;
  - `isLocal=true` → posts made on this server.
  - `count` is capped at 100, as in PeerTube.
- **Search:**
  - `/api/v1/search/videos` searches video titles and bodies, and takes `tagsOneOf`.
  - `/search/video-channels` searches communities.
  - `/search/video-playlists` is empty.
  - The `sepia_*` searches are NewPipe's own, sent to sepiasearch.org.
- **Empty answers** (`{total: 0, data: []}`, or PeerTube's empty shape): `/captions`, `/chapters`, the storyboards and
  `/api/v1/video-playlists/*`.
- **Web links** NewPipe shares are `/videos/watch/{uuid}` and `/w/{uuid}`. Both redirect to the post's page. Shared
  account and channel links are left as they are: PieFed's own `/u/` and `/c/` pages are what its web shows.
- **Errors** are PeerTube's shape `{error, status}`, never echoing input (as `app/mastodon/errors.failure`). An unknown
  or unreadable video answers 404, never telling hidden from missing.

## 4. Where it lives

- A new blueprint, `app/peertube/`, registered at `/api/v1` beside the Mastodon one. The two share no path that matters:
  - Mastodon's `/api/v1/accounts/{id}` takes a numeric id (an `int` route). PeerTube's `/accounts/{name}` takes a
    name, or `name@host`, which is never a bare number, so the router tells them apart.
  - Mastodon has no `/api/v1/videos`, `/video-channels`, `/config` or `/search/*` paths (its search is v2).
- Reads go through `app/reads/` only. No query of its own reaches a post another path would hide.
- It is served on every domain, each with its own posts and restrictions. Remote files are left out on onion domains
  (V1).
- Media and image paths are on the domain asked (an upload's file on its post's domain, plan C3).

## 5. Phases (M8: one branch each, merged when the harness flips its rows in two cold runs and the gates pass)

1. **W2.1a Read API.**
   - Builds §3 and §4 with one file per video.
   - The harness gains a NewPipe driver: add the instance, open the local kiosk, open the seeded video, and see the
     player reach the playing state.
   - NewPipe `view` flips.
2. **W2.1b Renditions** (V3).
   - After a video upload is probed `ready`, a background task writes H.264/AAC MP4 copies at 480p and 360p. It writes
     only sizes below the source, so a 480p source gains a 360p copy and a 360p source gains none.
   - The copies are recorded in the media record as `renditions[]` (height, width, bytes, path).
   - **On disk:** they sit beside the source, and S3 moves them as it moves the source.
   - **Removal:** they are removed wherever the source is (`media_local_path`, `Post.delete_dependencies`, the expiry
     sweep).
   - **Failure:** a failed rendition is logged and dropped. It never fails or delays the post, which publishes on the
     source as now.
   - **Where they appear:** `files[]` lists them, and so does the federated PeerTube Video's `url[]` (spec 2026-10-14
     §4.3). A peer's file Link list is otherwise unchanged.
   - Proved by tests of the pipeline and by NewPipe's resolution menu in the harness.

## 6. Owner rulings

- **D4** (2026-11-07): `/api/v1/config` `serverVersion` reports the PeerTube API level served, to every client,
  documented as that.
- **V1** (2026-11-09): videos are uploads plus remote posts whose URL is a video file. Remote files are left out on
  onion domains. Links to video sites are left out.
- **V2** (2026-11-09): every community is a channel, listing its video posts. An account is a person.
- **V3** (2026-11-09): uploads are also transcoded to lower sizes (480p, 360p), as phase 2 (W2.1b).

## 7. Security and privacy

- Anonymous only. Nothing a signed-out web reader of that domain could not see is listed, counted in a `total`, or
  found by id.
- A remote file's URL is the post's own, already shown on its web page. NewPipe fetching it tells that server the
  viewer's address, as the web page's player does. On an onion this never happens (V1).
- Every URL of this site in a response (files, image paths) is moved onto the domain asked, so no response names
  another of its domains. Comment HTML is passed as stored, as the Mastodon API passes status content.
- Transcoding runs ffmpeg on our own validated uploads only, through the existing `app/shared/ffmpeg` wrappers, with
  their limits. It never runs on remote files.

## 8. Out of scope

- HLS, live streams, captions, chapters, storyboards, playlists, view counting, and sepiasearch.
- Accounts, subscriptions, comments, ratings and uploads: W2.2 and W2.3.

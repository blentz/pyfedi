# Loops interoperability (both directions)

Date: 2026-10-30
Status: spike done, owner rulings L1-L4; awaiting owner review

Loops (joinloops, the federated short-video app by the Pixelfed author) carried out as Pixelfed was
(`2026-10-28-pixelfed-interop-design.md`): a local container, a spike, then this spec. Spike artefacts (gitignored):
`.superpowers/spike-loops/` (`upstream/` loops-server v1.0.0-beta.15 and joinloops/relay sources,
`report/source-analysis.md`, `report/findings.md`, `report/upstream-issue-U1.md`, `evidence/`).

## 1. Goal

- **In:** Loops videos reach hell.cloud as playable video posts, with their thumbnail, alt text and a clean title.
  They come from the Loops relay and from Loops accounts that hell.cloud users follow. Loops comments, edits and
  deletes follow.
- **Out:** a Loops user who follows a hell.cloud user sees that user's uploaded videos in their Loops Following feed
  as native Loops videos. Likes and comments from Loops come back into the PieFed thread.

### Owner rulings (2026-10-30)

- L1. Inbound = the Loops relay (`https://relay.loopsplatform.com`, through the existing relay admin page) plus
  ordinary user follows. No seeding bot and no API polling.
- L2. Outbound = users' own uploaded mp4 video posts, sent in the shape Loops accepts, the title folded into the
  content. Communities are out of scope on Loops (it follows `Person` actors only). Every Loops server's admin must
  allow our videos (`allow_video_posts`, off by default even between Loops servers); recorded, not ours to change.
- L3. Loops bug U1 (§2): PieFed re-sends a user's Follow of a Loops account once if it is still pending after
  5 minutes; the upstream issue is drafted for the owner to file.
- L4. The relay is verified with a harness stand-in that speaks the published relay JSON (§2).

Inherited: verification against local containers only, never public servers; 100% line and branch coverage on every
line this work adds or changes.

### Out of scope

Communities on Loops (L2); seeding Loops accounts by follow or by polling (L1: Loops drops every Application-signed
activity except from relays its admin subscribed to, so the Pixelfed `/actor` method cannot work); Loops starter
kits, quotes, DMs, duets/stitches; transcoding on our side (Loops downloads and transcodes our mp4 itself).

## 2. Current state (spike, Loops v1.0.0-beta.15 in the harness)

Works already, verified live: follows both ways (Accept handled); a Loops video arrives as `Create{Note}` with a
`video/mp4` `Document`, is stored in `microblogs` as `POST_TYPE_VIDEO` with the mp4 as `url`, and plays inline as a
`<video>`; Loops comments and replies arrive threaded; PieFed comments and likes reach Loops; a Loops delete removes
the post here. PieFed already records `instance.software = 'loops'`.

Found:

- **F1, fixed** on `fix/ffmpeg-threads` (07ec932ce, merged into `spike/loops`, not yet in blentz). The sandboxed
  thumbnail ffmpeg (512 MiB `RLIMIT_AS`) started one decoder thread per core; on the 32-core harness host a 720x1280
  or 1080x1920 upload failed its probe (`get_buffer() failed`) and the post was refused. Every phone-size video
  upload is likely failing on any many-core production host. Fix: `-threads 1` for `extract_frame` and
  `extract_cover`, with a real 1080x1920 regression test.
- **I1.** An inbound Loops video has no thumbnail: the object's `preview` (`{type: Image, url: {href, mediaType}}`,
  the jpg Loops made) is never read, so `post.image` stays empty: no poster on the player, nothing in card views.
- **I2.** The alt text (`attachment[].name`, also `preview.name`) is dropped.
- **I3.** The title derived from a Loops caption loses the hashtag and the space before it: content
  `<p>Loops probe one <a class="mention hashtag" …>#<span>interop</span></a></p>` gives `Loops probe oneinterop`
  (a sibling of Pixelfed P4, `microblog_content_to_title`, `app/utils.py:2255`).
- **O1.** A local user's video upload (a `video_channel` post) goes to the author's followers as PeerTube's
  `Create{Video}` whose `url` is a `Link` array (`send_peertube_video`, `app/shared/tasks/pages.py:169`). Loops
  answers 202 and drops it: its inbox stores only a `Note` (`CreateValidator`).
- **U1 (Loops bug).** Loops drops the first activity from any actor it has not stored yet: the new `Profile` is
  created without `status`, so `status !== 1` returns early
  (`ProcessInboxActivityWithVerification.php:484, :95`). A hell.cloud user's first Follow of a Loops account stays
  pending; the same Follow sent again is accepted at once.

The relay, verified with a stand-in (ruling L4, 2026-10-30): the published `joinloops/relay` (one commit,
d6c013cbd) cannot run, because its inbox job (`App\Jobs\ProcessInboxActivity`), its `AuthController` and all its
migrations are missing from the repository. It does publish the JSON it sends (`app/Services/RelayActor.php`): an
`Application` actor, an `Accept` wrapping the Follow, and an `Announce` of the bare object URI. `interop/relay_standin.py`
(`relay.test`) speaks exactly that. Verified live: Loops subscribes through its own `RelayService::subscribe` and
sends its public video Creates there; PieFed's `add_relay` detects it as LitePub style and is accepted; a video by a
Loops user nobody here follows arrives through the relay's Announce, is fetched from Loops by
`process_relayed_announce`, and is stored in `microblogs` as a video post with `relay_id` set. Not covered by the
stand-in: the real relay's moderation and filtering, and its invite-only setting (`restrict => true`), which keeps
our Follow pending until its admin allows hell.cloud.

## 3. Design

### 3.1 I1 + I2: thumbnail and alt text of an inbound video Note

When a stored post is `POST_TYPE_VIDEO` from a `Note` whose `url` came from a video attachment, and the object has a
`preview` naming an image (`url` a string, or an object or list with `href`, http(s), `mediaType` image/*), that
image becomes `post.image` (a `File` with `source_url`, `width`/`height` from `preview`). The alt text is the video
attachment's `name`, else `preview.name`, through `storable_alt_text`. Remote images follow
`cache_remote_images_locally` as every remote thumbnail does. The same rule runs on `Update`. Not Loops-specific:
any Note with a video attachment and a preview gets it.

### 3.2 I3: title from a caption with hashtag links

`microblog_content_to_title` keeps a hashtag link's text with one space before it: the caption above gives
`Loops probe one #interop`. The build step first pins where the `#` and the space are lost (the spike only saw the
result) with a failing test on the real Loops content.

### 3.3 O1: a Loops-shaped video for Loops followers

A new serializer, `loops_video(post)` in `app/activitypub/serializers/loops.py`, built from the same post as
`peertube_video`:

```json
{"id": "<post ap_id>", "type": "Note", "attributedTo": "<author>", "url": "<post page url, a string>",
 "content": "<p><b>title</b></p>\n<body html>", "sensitive": <nsfw>, "summary": null, "published": "…",
 "to": ["https://www.w3.org/ns/activitystreams#Public"], "cc": ["<author followers url>"],
 "attachment": [{"type": "Document", "mediaType": "video/mp4", "url": "<the mp4 url>", "name": "<alt or null>",
                 "width": w, "height": h}],
 "preview": {"type": "Image", "url": {"href": "<thumbnail url>", "mediaType": "image/jpeg"}, "width": …, "height": …},
 "tag": [<Hashtag tags as Mastodon gets them>]}
```

Rules from the source analysis: `object.url` must be a string (Loops names the post by it in its Likes and comment
`inReplyTo`, so PieFed must also resolve that URL to the post); the mp4 must be served on 443 and answer HEAD 2xx
with a Content-Length ≤ 100 MB; only `video/mp4`; the activity's `to`/`cc` must name the full Public URI and the
author's followers URL exactly; the Create is signed by the author.

Delivery: in `send_peertube_video`, an author-follower instance whose `software == 'loops'` gets
`Create{loops_video}` (`Update` on edit) instead of the PeerTube `Video`, signed by the author. A post Loops would
refuse is not sent to Loops at all: mime not `video/mp4`, or larger than 100 MB. Delete already embeds a Tombstone
(Pixelfed P2), which Loops accepts.

Inbound from Loops on our videos: Loops sends a comment only to the commenter's followers and mentioned accounts,
never to the video's author (source analysis §4), so a Loops comment on a hell.cloud video arrives only if a
hell.cloud user follows the commenter. Recorded as a Loops limitation. Likes go to the author's inbox and arrive.

### 3.4 U1: re-send a Follow that Loops dropped

When a local user follows a remote user on an instance with `software == 'loops'`, a Celery task is queued with a
5-minute countdown. If the `UserFollower` row is still pending (`is_accepted` None), it re-sends the same Follow
once. Nothing is queued for other software; an accepted, refused or removed follow cancels the re-send.

### 3.5 Relay (L1)

Nothing to build: the relay path works (§2). The owner adds `https://relay.loopsplatform.com` on the relay admin
page, then asks the relay admin to allow hell.cloud. A unit test pins `detect_relay` on the relay's real actor
document (LitePub style, its `/actor/inbox`), so a change on either side shows up in the suite.

### 3.6 Interop tests (`interop/test_loops.py`)

Live, in the harness: a Loops video arrives with its mp4, thumbnail, alt text and title (I1-I3); comments both ways;
a delete arrives; a video by a Loops user nobody here follows arrives through the stand-in relay; alice's video-channel upload reaches lucy's Following feed (Loops `videos` row, status 2) and an
edit updates it there; a Loops like on it reaches PieFed; alice's first Follow of a new Loops user ends accepted
without a manual re-follow (U1). Loops refuses uploads under 250 KB, so the harness clip is generated by
`interop/run.sh` with Loops' ffmpeg rather than committed.

## 4. Build order

1. F1 into blentz (done on its branch).
2. I3, then I1 + I2, TDD, one commit each.
3. O1: serializer, delivery branch, resolving Loops' `url` references back to the post.
4. U1 re-send.
5. Relay `detect_relay` pin.
6. `interop/test_loops.py`, then merge `spike/loops`.

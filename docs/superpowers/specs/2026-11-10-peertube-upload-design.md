# PeerTube upload: PeerTube's app uploads a video to PieFed

Date: 2026-11-10
Status: owner rulings U1-U4 given 2026-11-10 (§6): U2-U4 as recommended, U1 with Internal supported. Part of the API-completeness plan
(`docs/superpowers/plans/2026-11-07-api-completeness.md`, workstream W2.3). Builds on W2.1 (reading, renditions) and
W2.2 (login, `users/me` with `videoChannels`). Nothing here is built yet.

## 1. Goal

A person who may upload video to a PieFed video community on the web does so from PeerTube's own Android app: picks a
file, a channel (one of those communities), a title, and uploads. The video becomes a video post in that community,
goes through PieFed's media pipeline (A13: probed, made ready, renditions at 480p and 360p, W2.1b), federates as any
upload does, and the app shows it as its own. The person can then edit its details and delete it from the app.

## 2. What the app calls

Read on 2026-11-10 from the official app (Flutter, `framasoft/peertube/mobile-application` v2.3.0, commit 9bd3a4d),
`lib/features/video/manage/video_manage_datasource.dart` and `video_manage_service.dart`:

1. **Start:** `POST /api/v1/videos/upload-resumable`, JSON `{filename, name, channelId}`, headers
   `X-Upload-Content-Length` (the file's size) and `X-Upload-Content-Type`. Expects 201 and a `Location` header naming
   the upload (`...?upload_id=<id>`).
2. **Chunks:** `PUT /api/v1/videos/upload-resumable?upload_id=<id>`, `Content-Range: bytes <from>-<to>/<size>`, the
   bytes as `application/octet-stream`. A chunk that is not the last expects 308 with `Range: bytes=0-<last byte
   held>`; the last expects 200 and `{video: {id, shortUUID, uuid}}`. Chunks start at 1 MiB and grow to 100 MiB;
   PieFed's request limit (`MAX_CONTENT_LENGTH`) is 100 MiB.
3. **Resume:** the same PUT with `Content-Range: bytes */<size>` and no body: 308 with how much is held.
4. **Cancel:** `DELETE /api/v1/videos/upload-resumable?upload_id=<id>`: 204.
5. **Details:** `PUT /api/v1/videos/<uuid>`, JSON with any of `name`, `description`, `privacy`, `nsfw`, `tags`,
   `category`, `licence`, `language`, `commentsPolicy`, `downloadEnabled`, `support`: 204. Then, if one was picked,
   `PUT /api/v1/videos/<uuid>` as `multipart/form-data` with `thumbnailfile`.
6. **Delete:** `DELETE /api/v1/videos/<uuid>`: 204.

Before it offers an upload the app reads `users/me` (its `videoChannels` and `videoQuota`, both W2.2) and
`/api/v1/config`'s `video.file.extensions`.

## 3. How it is built

- **Who may upload where:** `channelId` is a community's id (V2). An upload is taken only where the web takes one,
  `app.shared.media.can_upload_media` (a moderator of a video community whose upload switch is on, A13.6); anywhere
  else is 403 before a byte is stored. `users/me`'s `videoChannels` (W2.2: the communities the person owns) also lists
  the video communities they may upload to, so the app offers every one.
- **Size and type:** the declared size must be within `upload_byte_limit('video')` (A13), refused at the start (413);
  the bytes received may not pass the declared size. The finished file is checked as the web's is
  (`store_uploaded_media`: its first bytes must prove a video type).
- **Storing the parts:** each upload is a row (who, the community, declared size and type, the title, when it began)
  and a file under a private directory, appended chunk by chunk at the offset it says. A chunk that does not start at
  the bytes held is answered 308 with what is held, as PeerTube does, so the app resumes from there.
- **Finishing:** the app sends the video's details (privacy among them) in a second request after the last byte, and
  a PieFed post's visibility is fixed when it is made (Amendment B), since it federates from then on. So the last byte
  only finishes the file: the upload is answered with a video uuid of its own (`video_uuid` keeps the post's for the
  post; an upload's is in a namespace of its own, `...-9000-<upload id>`), and the post is made when the details
  come, through `make_post` as the web's form makes one: one code path for the post, its media record, its
  federation and its probe. An upload whose start already carried the details (`privacy`, as PeerTube's API also
  takes there) is made at its last byte. A finished upload no details came for within 10 minutes is made with
  PeerTube's own default, public, by the maintenance task. The upload's uuid names its post from then on, for the
  app's later calls.
- **Details:** `PUT /videos/<uuid>` makes the post (above), or edits it through `edit_post`: `name` its title,
  `description` its body, `nsfw`, `tags` its tags; `privacy` is taken when the post is made, per U1, and refused as a
  change afterwards, as PieFed's visibility is. `category`, `licence`, `language`, `commentsPolicy`, `downloadEnabled` and
  `support` are not kept: PieFed has no such thing. The thumbnail becomes the post's image. Only the author may.
- **Deleting:** `DELETE /videos/<uuid>` deletes the post as its author would on the web (`delete_post`).
- **Errors:** PeerTube's problem+json with a `code`, as W2.2's (`app.peertube.auth.problem`).

## 4. Harness

Per U4 (a): the PeerTube app's `post` step uploads a short clip through its own screens. The harness pushes the
clip onto the emulator; the step picks it in Android's picker, names it, uploads it to tess's channel `tube`, and
checks PieFed has the video post. The comment it made before is now part of `view`.

## 5. Not in W2.3

Live streaming, captions, chapters, video passwords, scheduled publication, imports by URL or torrent, and studio
editing: the app hides each where `/api/v1/config` says it is off, which it does.

## 6. Decisions for the owner

- **U1. Privacy.** PeerTube's 1 Public, 2 Unlisted, 3 Private (the uploader alone), 4 Internal (this instance's users),
  5 Password protected; a PieFed post is public, unlisted, followers-only or direct. Recommendation: 1 public,
  2 unlisted; 3, 4 and 5 refused with a message saying PieFed has no such setting, rather than mapped to one that
  shows the video to other people than the uploader chose (followers-only is neither "only me" nor "this instance").
  **Ruled 2026-11-10: 1 public and 2 unlisted kept; 4 Internal supported; 3 and 5 refused.** Internal is a post
  stamped local-only (`Post.local_only`): signed-in readers of this server see it, signed-out ones and other servers
  never do, its replies are stamped too, and the last outbound gate (`app.local_only.outbound_refusal`) refuses any
  activity naming it. Read back, such a video's privacy is 4 Internal.
- **U2. Unfinished uploads.** Kept 24 hours from their last chunk, then removed by the hourly maintenance; a person
  may have at most 3 in progress, and the declared sizes of their unfinished uploads may not together pass 2× the video
  limit. Recommendation: as written. Or: no limit beyond the per-file one.
  **Ruled 2026-11-10: as recommended.**
- **U3. The quota the app is told.** `users/me` says `videoQuota: -1` (unlimited) today. PieFed has a per-file limit
  and no total. Recommendation: keep -1 and refuse an oversized file at the start with a message naming the limit;
  the app shows that message. Or: report the per-file limit as the quota, which the app would read as a total.
  **Ruled 2026-11-10: as recommended.**
- **U4. The harness.** (a) The PeerTube app's `post` step uploads a short video through the app's own screens: the
  harness puts a clip on the emulator, the step picks it in Android's picker, names it, uploads to tess's channel, and
  checks PieFed has the post, while commenting moves into `view`. (b) Keep `post` as the comment and check the upload
  at the API level only. Recommendation: (a): the app's upload is the feature, and the picker is the only way to it.
  **Ruled 2026-11-10: (a).**

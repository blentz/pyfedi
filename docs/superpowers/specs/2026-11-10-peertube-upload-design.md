# PeerTube upload: PeerTube's app uploads a video to PieFed

Date: 2026-11-10
Status: draft; owner rulings U1-U4 open (§6). Part of the API-completeness plan
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
  else is 403 before a byte is stored. `users/me`'s `videoChannels` already lists those communities.
- **Size and type:** the declared size must be within `upload_byte_limit('video')` (A13), refused at the start (413);
  the bytes received may not pass the declared size. The finished file is checked as the web's is
  (`store_uploaded_media`: its first bytes must prove a video type).
- **Storing the parts:** each upload is a row (who, the community, declared size and type, the title, when it began)
  and a file under a private directory, appended chunk by chunk at the offset it says. A chunk that does not start at
  the bytes held is answered 308 with what is held, as PeerTube does, so the app resumes from there.
- **Finishing:** at the last byte, the file goes through `make_post` as a video post with the upload's title, exactly
  as the web's form does: one code path for the post, its media record, its federation and its probe. The answer is
  the post's PeerTube ids (`video_uuid(post)`, W2.1).
- **Details:** `PUT /videos/<uuid>` edits the post through `edit_post`: `name` its title, `description` its body,
  `nsfw`, `tags` its tags, `privacy` per U1. `category`, `licence`, `language`, `commentsPolicy`, `downloadEnabled` and
  `support` are not kept: PieFed has no such thing. The thumbnail becomes the post's image. Only the author may.
- **Deleting:** `DELETE /videos/<uuid>` deletes the post as its author would on the web (`delete_post`).
- **Errors:** PeerTube's problem+json with a `code`, as W2.2's (`app.peertube.auth.problem`).

## 4. Harness

Per U4.

## 5. Not in W2.3

Live streaming, captions, chapters, video passwords, scheduled publication, imports by URL or torrent, and studio
editing: the app hides each where `/api/v1/config` says it is off, which it does.

## 6. Decisions for the owner

- **U1. Privacy.** PeerTube's 1 Public, 2 Unlisted, 3 Private (the uploader alone), 4 Internal (this instance's users),
  5 Password protected; a PieFed post is public, unlisted, followers-only or direct. Recommendation: 1 public,
  2 unlisted; 3, 4 and 5 refused with a message saying PieFed has no such setting, rather than mapped to one that
  shows the video to other people than the uploader chose (followers-only is neither "only me" nor "this instance").
- **U2. Unfinished uploads.** Kept 24 hours from their last chunk, then removed by the hourly maintenance; a person
  may have at most 3 in progress, and the declared sizes of their unfinished uploads may not together pass 2× the video
  limit. Recommendation: as written. Or: no limit beyond the per-file one.
- **U3. The quota the app is told.** `users/me` says `videoQuota: -1` (unlimited) today. PieFed has a per-file limit
  and no total. Recommendation: keep -1 and refuse an oversized file at the start with a message naming the limit;
  the app shows that message. Or: report the per-file limit as the quota, which the app would read as a total.
- **U4. The harness.** (a) The PeerTube app's `post` step uploads a short video through the app's own screens: the
  harness puts a clip on the emulator, the step picks it in Android's picker, names it, uploads to tess's channel, and
  checks PieFed has the post, while commenting moves into `view`. (b) Keep `post` as the comment and check the upload
  at the API level only. Recommendation: (a): the app's upload is the feature, and the picker is the only way to it.

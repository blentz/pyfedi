# Pixelfed interoperability (both directions)

Date: 2026-10-28
Status: spike done, owner rulings R1-R7 given in conversation; spec awaiting owner review

Phase 3 of `docs/superpowers/specs/2026-10-01-activitypub-interop-design.md` (D10 Pixelfed MVP: multi-image album,
alt text, sensitive blur), carried out the way `2026-10-14-native-federation-design.md` did Mastodon and PeerTube:
a local container of the target app, a spike, then a spec. Spike artefacts (gitignored): `.superpowers/spike-pixelfed/`
(`report/source-analysis.md`, `report/findings.md`, probes and evidence).

## 1. Goal

- **Out:** a Pixelfed user follows a first-domain user and sees that user's image posts in their home feed as
  native Pixelfed posts: every image, its alt text, the sensitive flag, and edits and deletes as they happen.
  Replies, likes and boosts from Pixelfed come back into the PieFed thread.
- **In:** a first-domain user follows a Pixelfed user and gets that user's posts as PieFed posts: every image of an
  album in order with its alt text, a content warning as a collapse with the image blurred, edits and deletes.

### Owner rulings (2026-10-28)

- R1. Both directions.
- R2. Communities are out of scope on Pixelfed. Pixelfed can follow a PieFed Group but shows none of its posts: its
  inbox stores only a `Create` of a `Note`, and an `Announce` reaches no feed. Native display on Pixelfed covers a
  user's own posts only.
- R3. An image that is not JPEG, PNG or GIF goes out as a JPEG copy made at upload; the outbound attachment names the
  copy. Video stays a recorded limitation (Pixelfed's default media list has no `video/mp4`).
- R4. Inbound content warning: the current rule stays (title = the warning text, `app/models.py:3218`; body in
  `<details class="content_warning">`; images blurred through `nsfw`). Nothing to build.
- R5. Local users get album posts: several images, each with alt text, stored in the existing `post.gallery` and
  sent as several attachments.
- R6. The album view stays the existing grid plus lightbox. No carousel.
- R7. Album size is an admin setting, default 4. Mastodon shows only the first 4; accepted.

Inherited from the native-federation track: verification against local containers only, never public servers; 100%
line and branch coverage on every line this work adds or changes.

### Out of scope

Communities on Pixelfed (R2), text-only posts in Pixelfed home feeds (Pixelfed shows a post without media on the
profile only), video to Pixelfed (R3), Pixelfed Stories (sent only to peers whose nodeinfo says `pixelfed`),
Pixelfed collections (not federated), an album carousel (R6).

## 2. Current state (from the spike, Pixelfed v0.15.2)

Works already, verified live: follows both ways; a `microblogs` post with one JPEG reaches a Pixelfed home feed with
its alt text (once P1 below is fixed); Pixelfed replies, likes, boosts (`post_boost`), edits and deletes reach PieFed;
PieFed likes and replies reach Pixelfed; a Pixelfed album arrives with every image in order and each alt text
(`post.image` plus `post.gallery`); a sensitive Pixelfed post arrives as R4 describes.

Defects found (PieFed):

- **P1, fixed** on `fix/instance-shared-inbox` (c9cf751fa, migration f706cd4f9 `e8b4d2a6c1f3`). Instances kept a guessed
  `https://<host>/inbox` and every follower fan-out posted there; Pixelfed's shared inbox is `/f/inbox`, so every
  Create, Update and Delete to a Pixelfed follower was a 405. An actor's trusted sharedInbox now becomes its
  instance's inbox, and a migration backfills stored instances. Verified on a fresh harness.
- **P2.** An outbound `Delete` names its object as a string. Pixelfed acts only on an embedded object of type
  `Tombstone`, `Person` or `Story` (`FederationController.php:194-230`), so a deleted post stays up there.
- **P3.** An inbound `Create` that names no community is first offered to `process_chat`, which logs a FAILURE row
  ("ChatMessage target is not local") and returns False; the same activity then succeeds
  (`app/activitypub/routes.py:1786-1788, 3755`). Every Pixelfed post, and likely every Mastodon post, logs a false
  failure.
- **P4 (cosmetic).** A title derived from a caption keeps a double space where a hashtag link was
  (`Render album  #interop`).

Gaps against the goal:

- `note_attachments` (`app/activitypub/serializers/microblog.py:108`) sends `post.image` only. Local posts have one
  image; `post.gallery` is filled only from inbound posts (`set_post_gallery`).
- Pixelfed validates attachments all-or-nothing (`Helpers.php:164-190`): `type` Document or Image, `mediaType` in
  its list (default jpeg/jpg/png/gif), width and height 1..5000 if present. PieFed sends the uploaded format
  (`MEDIA_IMAGE_FORMAT` empty by default), so a WebP, AVIF or HEIC upload makes the whole post vanish on Pixelfed.

## 3. Design

### 3.1 P2: Delete with an embedded Tombstone

Every outbound Delete of a post or reply names its object as `{"id": <ap_id>, "type": "Tombstone"}` instead of the
bare id. Mastodon, Lemmy and PieFed accept both forms; PieFed's own inbound Delete handling must keep accepting both
(check `_id_of` use). Actor self-deletes keep the string form (Pixelfed treats string-object = actor as self-delete).
Interop proof: delete on PieFed, gone on Pixelfed and still gone on Mastodon.

### 3.2 P3: no false chat failure

`process_chat` declines without logging when it is reached as the fallback of a Create/Update that may be content
(the caller goes on and logs the real outcome). It keeps logging when the object is a `ChatMessage`.

### 3.3 P4: title spacing

Collapse runs of whitespace in a title derived from a caption.

### 3.4 JPEG copy for outbound images (R3)

- At upload (`app/shared/upload.py`), when the stored file's format is not JPEG, PNG or GIF (after any
  `MEDIA_IMAGE_FORMAT` conversion), write a JPEG copy beside it, metadata stripped as the original is, and record it
  on `File` in a new nullable column (e.g. `compat_path`, with its URL derived as `source_url` is; S3 when
  `store_files_in_s3()`). One migration.
- `note_attachments` names the copy's URL with `mediaType: image/jpeg` when there is one, else the original. The
  copy is deleted with the file (every path that deletes `file_path`).
- Existing uploads get no copy (no backfill); their posts keep sending the original.
- Applies to every image `note_attachments` sends, so to album images too.

### 3.5 Albums (R5-R7)

- Site setting `max_album_images` (admin, default 4, floor 1). The web post form and the alpha API post create and
  edit accept up to that many images on an image post, each with its own alt text. The first is `post.image`, the
  rest go to `post.gallery` with `weight` 1..n (the inbound shape, so rendering, teasers and the lightbox need no
  change; `gallery_count` kept in step).
- Edit: images can be added, removed and reordered; alt text edited per image. An edit sends an `Update` whose Note
  carries the new attachment list (Pixelfed replaces media on Update after a HEAD check of each URL).
- `note_attachments` emits one `Document` per image, in order, each with `mediaType`, `url`, `name` (alt),
  width and height. The `Page` a community post federates as carries the same list (`attachment`) with `image` the
  first one (threadiverse peers show the first; PieFed peers rebuild the gallery).
- Uploads count against the same size and type checks as today's single image (`R207` 10 MB cap per image).
- Open for review: whether albums are offered only in `microblogs` (where Pixelfed shows them) or on every image post.
  The spec assumes every image post, since the storage and rendering are shared.

### 3.6 Interop tests

`interop/test_pixelfed.py`, from the spike probes, using the `pixelfed_erin` token (Mastodon-compatible API):
outbound single image and album reach erin's home feed with every alt text; a WebP upload arrives as JPEG; edit and
delete show on Pixelfed; Pixelfed reply, like and boost reach PieFed. Inbound: album order and alt texts, CW as R4,
edit and delete. Waits are bounded; Pixelfed's queues can take over 45 s to fan a post out.

## 4. Build order

0. Harness: Pixelfed in `compose.interop.yaml` (done, `spike/pixelfed` 4a3d7a6dc, 094e7733e).
1. P1 (done, `fix/instance-shared-inbox`). Deploy on its own: production Pixelfed followers get nothing until it ships.
2. P2, P3, P4: one commit each, test first.
3. JPEG copy (3.4), with its migration.
4. Albums (3.5): model and setting, then web form, then API, then serializers.
5. `interop/test_pixelfed.py` grows with each step; the full suite and the interop suite pass at each merge.

## 5. Harness notes

Pixelfed refuses peers on private addresses with no setting to allow them; `interop/pixelfed_harness.sh` patches
`Helpers::isPublicIp` for 10.90.0.0/16 at container start (harness only). Deliveries happen only with
`APP_ENV=production` and Horizon running. Passport refuses a token whose user id equals its client id
(`TokenGuard.php:146`); `interop/seed_pixelfed.php` renumbers the personal client to 100.

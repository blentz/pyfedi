# Connected accounts: publish to Bluesky, Facebook, Instagram and YouTube

A member links their PieFed account to accounts on other social services. When they post on PieFed, the post is
also published, reshaped for each service, to the connected accounts it fits:

| Service | Gets | Shape |
|---|---|---|
| Bluesky | every eligible post | status update: text, up to 4 images or 1 video, or a link card |
| Facebook (a Page) | every eligible post | status update: text and link, photos |
| Instagram | posts with images (image, gallery, microblog with pictures) | Pixelfed-shaped: one photo or a carousel, caption |
| YouTube | posts with an uploaded video | PeerTube-shaped: the video, title, description, tags |

## 1. Rulings (owner, 2026-11-13)

- **C1 Facebook = Pages only.** Facebook's API has not been able to post to a personal profile since 2018 (and to
  Groups since 2024). A member connects a Page they manage; posts go to the Page's feed.
- **C2 source posts: all the member's own local posts.** Not only the microblogs community. Text-bearing services take
  every eligible post; YouTube takes those with an uploaded video (any community, video channels included);
  Instagram takes those with images or a gallery.
- **C3 per-post control: automatic default plus checkboxes.** Each connection has "send my posts here by default".
  The post forms show one checkbox per connected account the post fits, pre-ticked from that default.
- **C4 edits and deletes sync where the API allows.** See §7 for what each service allows.
- **C5 Bluesky connects by atproto OAuth.** No app passwords.

## 2. Platform facts that bound the design (researched 2026-11-13; spike P0 re-checks the open ones)

- **Facebook**: Page token with `pages_manage_posts` (+ `pages_read_engagement`, `pages_show_list`); needs Meta App
  Review and Business Verification. `POST /{page-id}/feed` (message, link), `/{page-id}/photos`, multi-photo via
  unpublished photos + `attached_media`. Page posts can be edited (`POST /{post-id}` message) and deleted.
- **Instagram**: "Instagram API with Instagram Login", Professional (Business/Creator) accounts only, scope
  `instagram_business_content_publish`, App Review. Media is not uploaded: Meta **fetches it from a public HTTPS URL**
  (JPEG). Carousel up to 10 items. 50 to 100 published posts per 24h per account (read the live figure from
  `GET /{ig-id}/content_publishing_limit`). Caption up to 2200 characters; links in captions are not clickable.
  Open (P0): whether media delete and caption edit are available through this API.
- **YouTube**: Data API v3 `videos.insert` (resumable), scope `youtube.upload` (sensitive: Google OAuth verification).
  Uploads from projects that have not passed YouTube's API audit are **forced to private**. Quota cost per upload
  has changed during 2025-26 (sources say 1600, 100 or a separate upload bucket). Open (P0): read the live quota
  table and the project's audit state. `videos.update` (snippet) edits, `videos.delete` deletes.
- **Bluesky**: atproto OAuth (PAR, PKCE, DPoP), confidential client using `private_key_jwt` / ES256, client metadata
  JSON published at a public URL, works with any PDS (bsky.social or self-hosted). Post = `app.bsky.feed.post` via
  `com.atproto.repo.createRecord`, media via `uploadBlob`. 300 graphemes; up to 4 images (about 2 MB each) or one
  video (video service; limits read live from `app.bsky.video.getUploadLimits`). Posts cannot be edited (the app
  shows no edits); `deleteRecord` deletes.

## 3. Who sets what up

- **Admin, per instance.** Secrets live in the environment, like the existing Google/Mastodon/Discord login OAuth
  (`config.py`):
  - `YOUTUBE_OAUTH_CLIENT_ID` / `YOUTUBE_OAUTH_SECRET` (may be the same Google client as login; it asks a separate
    consent for `youtube.upload`).
  - `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` (Facebook Login for Business).
  - `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET` (Instagram Login's own app id).
  - `BLUESKY_OAUTH_KEY` (an ES256 private JWK; `flask connections bluesky-key` prints a new one). No registration:
    the client id is our metadata URL.
  - `CONNECTIONS_SECRETS_KEY` (Fernet key(s), comma separated, newest first) seals members' tokens.
  `app/payments/credentials.py`'s seal/unseal is generalised to take the config name; payments keep
  `PAYMENT_SECRETS_KEY`.
- **Admin site settings** gain one switch per service, offered only when that service's credentials are set.
  A service switched off hides its Connect button and stops sending; existing connections are kept.
- **Member**: `/user/settings/connections` lists the services the admin enabled, with Connect / Disconnect and,
  per connection, the default-on switch, the account's name, and its state ("working" or "needs reconnecting").
  Facebook asks which Page after login when the person manages more than one.

## 4. Data

- `social_connection`: id, user_id (cascade), service (`bluesky|facebook|instagram|youtube`), remote_id (DID, page
  id, IG user id, channel id), remote_name (handle / page / channel title, for display), sealed_tokens (Fernet text:
  access, refresh, expiry, DPoP key and PDS URL for Bluesky), default_on (bool, default true), state
  (`ok|reauth`), last_error (short, no secrets), created_at. Unique (user_id, service, remote_id).
- `social_crosspost`: id, post_id (cascade), connection_id (set null on disconnect), service, remote_id (record URI /
  post id / media id / video id), remote_url, state (`queued|sent|failed|deleted|skipped`), attempts, last_error,
  sent_at, edited_at. Unique (post_id, connection_id).
- Disconnecting revokes the token at the service where it can, deletes the connection, and leaves the remote copies
  alone (a person who wants them gone deletes the post first).

## 5. Which posts go (C2, C3)

A post is eligible for a connection when all hold:
- the author is local, owns the connection, and the post is theirs;
- status is published (scheduled posts go when they publish; drafts and processing media wait for the publish);
- visibility is `public` (unlisted, followers-only and direct never leave the instance);
- the community is not private and not `local_only`, and not on a private instance;
- the service fits the post (table above);
- **default**: NSFW/NSFL posts go only to Bluesky, carrying the self-labels `sexual` / `graphic-media`; Facebook,
  Instagram and YouTube skip them (their content rules), shown to the author as "skipped: NSFW".

The create forms (web, all post types) get a "Also post to" fieldset with one checkbox per eligible connection,
pre-ticked by `default_on`. The alpha API's post create takes optional `crosspost_to: [connection_id]`; absent means
the defaults. Mastodon, Lemmy and Pixelfed API clients cannot ask, so the defaults apply.

Send happens from `make_post` (after commit, beside `after_post_create`) and from the scheduled-post publish path,
as one Celery task per (post, connection). A post that is still `POST_STATUS_PROCESSING` sends when processing
finishes.

## 6. Shapes

All text is made from the post's title and Markdown body as plain text (`html_to_text`), with the post's own URL on
its public clearnet domain (never an onion address) as the link back.

- **Bluesky**: `title\n\nbody`, cut at 300 graphemes with "…" plus the link when longer; link facets with UTF-8
  byte offsets; hashtags as tag facets; `langs` from the post language. Embed: an uploaded video, else up to 4 images
  with alt text (re-encoded under the size cap), else a link post's external card (title, description, thumbnail).
  A gallery over 4 sends the first 4 and the link. Content warning: the text becomes "CW: <warning>" + link, no media.
- **Facebook Page**: message = title, body, link; link posts pass `link`; image → `/photos`; gallery →
  multi-photo (up to 10); video posts go as text + link (no Page video upload).
- **Instagram**: one image → single media container; gallery → carousel (first 10, caption notes "+N more" and the
  link). Caption = title, body, hashtags from post tags, link as text, cut at 2200. Each image is served to Meta as a
  JPEG at `/connections/media/<token>.jpg`: a signed URL on the public clearnet domain, valid one hour, for that one
  file only. Alt text goes in `alt_text`.
- **YouTube**: resumable upload of the stored original video (not the transcodes); title = post title (100 chars;
  untitled microblog → first line of body); description = body + link (5000 chars); tags = post tags;
  `selfDeclaredMadeForKids` = false; privacy `public` (if YouTube forces private for an unaudited project, the
  crosspost row records it and the settings page says so). Thumbnail: the post's poster frame via
  `thumbnails.set`.

## 7. Edits and deletes (C4)

| | Edit | Delete / mod removal | Restore |
|---|---|---|---|
| Bluesky | no (not editable) | `deleteRecord` | sends again as a new post |
| Facebook | message updated | deleted | sends again |
| Instagram | caption if P0 finds it possible, else no | deleted if P0 finds it possible, else recorded as "delete by hand" with a link | sends again |
| YouTube | title, description, tags updated | `videos.delete` | sends again |

Edits that change media do not resend media. An edit that makes a post ineligible (visibility narrowed, NSFW
added) deletes its copies. Purging an account deletes its copies and connections.

## 8. Sending, failure and tokens

- Celery task `crosspost.send(post_id, connection_id)`; idempotent on the `social_crosspost` row (a `sent` row is not
  sent again). Edit and delete tasks are separate and act only on `sent` rows.
- 429 and 5xx retry with backoff (honouring Retry-After), up to 5 tries, then `failed`. Other 4xx are `failed` at
  once with the service's error message (trimmed, no tokens).
- Expired access tokens refresh first. A refresh or auth failure sets the connection to `reauth`, notifies the member
  once, and fails the row. Nothing retries on a `reauth` connection.
- All outbound HTTP goes through the pinned client (R162). Tokens are never logged.
- The author sees on their post (author-only): "Posted to Bluesky ↗ · Failed on YouTube: <reason> [Retry]".

## 9. Testing

- Unit tests per shape builder (pure functions: post → payload), grapheme/byte-offset edge cases, eligibility table.
- Each service client is tested against a local fake HTTP server that implements that service's documented calls
  and errors. No test touches the real services.
- **Bluesky end to end**: the interop harness gains a self-hosted PDS container (`bluesky-social/pds`) on remote-gpu;
  connect by OAuth, post, read back, delete.
- Fuzz targets for the settings and connect-callback routes (atheris, as the route track).
- Gates as usual: full suite, coverage floors, 100% line+branch on changed lines.
- Facebook, Instagram and YouTube need the owner's real apps for a live check; each phase ends with one.

## 10. Phases

- **P0 spike**: atproto OAuth against a local PDS; read the live Instagram and YouTube docs for the open facts in §2;
  findings rewrite §2, §6, §7 and the owner reviews.
- **P1 framework**: models + migration, sealing, settings page, admin switches, eligibility, form checkboxes, API
  field, dispatch, status display, edit/delete plumbing, fake-server harness.
- **P2 Bluesky.** **P3 Facebook Pages.** **P4 Instagram.** **P5 YouTube.** Each is merged on its own once its
  gates and live check pass.

## 11. Owner to-dos (outside the code)

- Google Cloud project: enable YouTube Data API v3, OAuth consent screen with `youtube.upload`, Google verification,
  and the YouTube API audit (without it uploads land private).
- Meta developer app: Facebook Login for Business with the Page permissions, Instagram API with Instagram Login,
  Business Verification and App Review for both.
- Set the environment variables in §3 in `.env.quadlet`.

## 12. Out of scope

Reading back from these services (replies, likes, comments), Instagram Reels/Stories, Facebook profiles and Groups,
X/Twitter, Threads, TikTok, LinkedIn, sending comments (only posts), and bridging (Bridgy Fed is the member's own
choice and needs nothing from us).

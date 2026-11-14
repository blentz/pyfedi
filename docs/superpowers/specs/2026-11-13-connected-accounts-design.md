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
- **C6 Instagram: both APIs** (§13). Facebook Login when the account is linked to a Page the member manages
  (deletes sync), Instagram Login otherwise (deletes by hand). Rows record which API made them.

## 2. Platform facts that bound the design (P0, 2026-11-13: official docs read live, Bluesky run end to end; §13)

- **Facebook**: Page token with `pages_manage_posts` (+ `pages_read_engagement`, `pages_show_list`); needs Meta App
  Review and Business Verification. `POST /{page-id}/feed` (message, link), `/{page-id}/photos`, multi-photo via
  unpublished photos + `attached_media`. Page posts can be edited (`POST /{post-id}` message) and deleted
  (not re-read in P0; the P3 live check confirms).
- **Instagram** (IG Media reference, updated 2026-10-29): Professional (Business/Creator) accounts only, App Review.
  Media is not uploaded: Meta **fetches it from a public HTTPS URL** (JPEG). Carousel up to 10 items. 50 to 100
  published posts per 24h per account (read the live figure from `GET /{ig-id}/content_publishing_limit`). Caption up
  to 2200 characters; links in captions are not clickable.
  - **A caption cannot be edited by any API.** The only updatable field of a published media is `comment_enabled`.
  - **Delete exists only through "Instagram API with Facebook Login"** (`DELETE /{ig-media-id}` on
    graph.facebook.com, permissions `instagram_basic` + `instagram_manage_contents`, since May 2026), and only when the
    member manages the Facebook Page the Instagram account is linked to. Carousels delete whole. The "Instagram
    Login" variant (no Page needed) has no delete. Which variant we use is ruling C6 (§13).
- **YouTube** (quota calculator and revision history, read live):
  - **Uploads are no longer forced private.** The 2020 rule for unaudited projects was reversed on 2026-11-02;
    an upload with no `privacyStatus` defaults to public. No API audit is needed.
  - **`videos.insert` has its own bucket: 100 uploads a day per Google project**, 1 unit each, reset at midnight
    Pacific. Every member of an instance shares that one project, so it is an instance-wide daily allowance.
    Everything else shares 10,000 units a day: `videos.update` 50, `videos.delete` 50, `thumbnails.set` 50.
  - Files up to 256 GB, `video/*`. `youtube.upload` is a sensitive scope: Google OAuth app verification is still
    needed before strangers can connect (the consent screen warns until then).
- **Bluesky** (PDS 0.4.5051 in the harness, 25/25 spike steps):
  - Confidential client with `private_key_jwt` / ES256, PAR, PKCE, DPoP: works as documented.
  - **The client id must be on a public domain.** The PDS refuses a client id whose TLD is `test`, `local`,
    `localhost`, `invalid` or `example`; it fetches the metadata with SSRF protection (no private addresses). An onion
    or LAN-only PieFed cannot be a Bluesky client.
  - **Granular scopes work and are enforced**: `atproto repo:app.bsky.feed.post?action=create&action=delete blob:*/*`
    is granted as asked; a profile write or a `putRecord` (needs `action=update`) is refused 403 `ScopeMissingError`.
  - Tokens are DPoP-bound, access lasts 1 hour, the refresh token rotates on every use.
  - **Replaying a used refresh token revokes the whole session**: the newest access and refresh tokens both die.
    Two workers refreshing one connection at once would silently disconnect it (§8).
  - Text over 300 graphemes is refused 400. Posts cannot be edited (and we do not ask for `update`);
    `deleteRecord` deletes. `/oauth/revoke` ends the session.
  - Video was not run (the harness has no video service): `app.bsky.video.uploadVideo` needs an `rpc:` scope for the
    video service and service auth; P2's live check settles it, and a video post falls back to text + link until then.

## 3. Who sets what up

- **Admin, per instance.** Secrets live in the environment, like the existing Google/Mastodon/Discord login OAuth
  (`config.py`):
  - `YOUTUBE_OAUTH_CLIENT_ID` / `YOUTUBE_OAUTH_SECRET` (may be the same Google client as login; it asks a separate
    consent for `youtube.upload`).
  - `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` (Facebook Login for Business).
  - `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET` (Instagram Login's own app id).
  - `BLUESKY_OAUTH_KEY` (an ES256 private JWK; `flask connections bluesky-key` prints a new one). No registration:
    the client id is `https://<SERVER_NAME>/connections/bluesky/client-metadata.json`, its keys at
    `/connections/bluesky/jwks.json`, its redirect `/user/settings/connections/bluesky/callback` (every service's is `/user/settings/connections/<service>/callback`), all on the instance's main public
    domain (never an onion or extra domain: §2). A member on another domain is sent through the main domain to
    connect. Bluesky needs `SERVER_NAME` on a public TLD (a PDS refuses the client id otherwise, §2); env.sample says
    so, and PieFed does not check it (P2, §14).
  - `CONNECTIONS_SECRETS_KEY` (Fernet key(s), comma separated, newest first) seals members' tokens.
  `app/payments/credentials.py`'s seal/unseal is generalised to take the config name; payments keep
  `PAYMENT_SECRETS_KEY`.
- **Admin site settings** gain one switch per service, offered only when that service's credentials are set.
  A service switched off hides its Connect button and stops sending; existing connections are kept.
- **Member**: `/user/settings/connections` lists the services the admin enabled, with Connect / Disconnect and,
  per connection, the default-on switch, the account's name, and its state ("working" or "needs reconnecting").
  Facebook asks which Page after login when the person manages more than one.

## 4. Data

- `social_connection`: id, user_id (cascade), service (`bluesky|facebook|instagram|youtube`), variant (Instagram:
  `facebook_login|instagram_login`; null otherwise), remote_id (DID, page
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

- **Bluesky**: `title\n\nbody`, cut at 300 graphemes (counted as code points, never fewer: P2, §14) with "…" plus the link when longer; link facets with UTF-8
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
  `selfDeclaredMadeForKids` = false; privacy `public` (the channel's own settings can still make it private; the
  crosspost row records the privacy YouTube reports). Thumbnail: the post's poster frame via `thumbnails.set`.

## 7. Edits and deletes (C4)

| | Edit | Delete / mod removal | Restore |
|---|---|---|---|
| Bluesky | no (not editable) | `deleteRecord` | sends again as a new post |
| Facebook | message updated | deleted | sends again |
| Instagram | no (no API edits a caption) | via Facebook Login: deleted; via Instagram Login: recorded as "delete by hand" with a link | sends again |
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
- **One refresh at a time per connection** (Bluesky revokes the session on a replayed refresh token, §2): the refresh
  runs under `SELECT ... FOR UPDATE` on the connection row, re-reads the tokens after taking the lock (another worker
  may have refreshed already), and commits the new tokens before any use. Every service gets the same rule.
- Bluesky keeps per connection, sealed: the DPoP private key (one per connection, never reused across members), the
  PDS URL and the authorization server issuer. DPoP nonces are kept per server in each process's memory, not sealed: a stale one costs one retry (P2).
- **YouTube's daily allowance** (100 uploads per instance, §2): a counter in Redis keyed by the Pacific date. When it
  is spent, YouTube rows wait as `queued` with "waiting for YouTube's daily upload allowance", and a beat task
  sends them after midnight Pacific, oldest first. A 403 `quotaExceeded` also parks the row this way.
- All outbound HTTP goes through the pinned client (R162). Tokens are never logged.
- The author sees on their post (author-only): "Posted to Bluesky ↗ · Failed on YouTube: <reason> [Retry]".

## 9. Testing

- Unit tests per shape builder (pure functions: post → payload), grapheme/byte-offset edge cases, eligibility table.
- Each service client is tested against a local fake HTTP server that implements that service's documented calls
  and errors. No test touches the real services.
- **Bluesky end to end**: the interop harness has a private PLC directory and a PDS (P0, `interop/run.sh
  bluesky-spike`); the P2 test connects the harness PieFed by OAuth, posts, reads back, deletes. The harness client
  domain is `*.piefed.internal` (`.internal` is reserved for private use and not on the PDS's local-TLD list), and
  `PDS_DEV_MODE` lets the PDS fetch from the harness's private addresses.
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

- Google Cloud project: enable YouTube Data API v3, OAuth consent screen with `youtube.upload`, Google OAuth app
  verification. (The YouTube API audit is no longer needed for public uploads, §2; it is the way to ask for more than
  100 uploads a day.)
- Meta developer app: Facebook Login for Business with the Page permissions, Instagram API with Instagram Login,
  Business Verification and App Review for both.
- Set the environment variables in §3 in `.env.quadlet`.

## 12. Out of scope

Reading back from these services (replies, likes, comments), Instagram Reels/Stories, Facebook profiles and Groups,
X/Twitter, Threads, TikTok, LinkedIn, sending comments (only posts), and bridging (Bridgy Fed is the member's own
choice and needs nothing from us).

## 13. P0 spike (2026-11-13)

**Bluesky, run end to end.** `interop/run.sh bluesky-spike` (or `interop/run_remote.sh bluesky-spike`) starts a
private PLC directory (`plc.test`, so no DID ever reaches plc.directory), a PDS (`pds.test`, handles `*.pds.test`)
and `interop/spike_bluesky_oauth.py`, which plays PieFed's part as a confidential client at
`bskyclient.piefed.internal`: handle → DID → PDS → authorization server, PAR, sign-in on the PDS page (Playwright),
code exchange, uploadBlob, a post with an image, a link facet and a self-label, the 300-grapheme refusal, scope
refusals, refresh, delete, revoke, and a second connection to test a refresh replay. 25/25 steps passed; the report
and screenshots are in `interop/spike_bluesky/`. First runs failed on two findings now in §2: the image's entry is
`index.ts` (Node 24), and the PDS refuses a client id on a `.test` TLD.

**Instagram and YouTube, from the official docs read live** (IG Media reference updated 2026-10-29, IG changelog,
YouTube quota calculator and revision history). Findings are in §2.

**What changed in this spec:** §2 rewritten; §3 Bluesky client URLs on the main public domain only; §6 YouTube
privacy; §7 Instagram row; §8 one refresh at a time, per-connection DPoP keys, YouTube's daily allowance; §9 the
harness; §11 no YouTube audit.

**C6 (owner ruling 2026-11-13): offer both Instagram APIs.** The connections page offers "Connect Instagram through
Facebook" (option A, deletes sync) and "Connect Instagram directly" (option B, no Page needed, deletes by hand).
Both need `INSTAGRAM_APP_ID` for B; A uses the Facebook app. The two options were:

- **A. Instagram API with Facebook Login (recommended).** The member signs in with Facebook and picks the Instagram
  account linked to a Page they manage. Deletes sync (C4). It uses the same Meta app and login as Facebook Pages
  (`FACEBOOK_APP_ID`; no `INSTAGRAM_APP_ID`), so one App Review covers both, adding `instagram_basic`,
  `instagram_content_publish` and `instagram_manage_contents`. Only Instagram accounts linked to a Facebook Page can
  connect.
- **B. Instagram API with Instagram Login.** The member signs in with Instagram directly, and no Facebook Page is
  needed. Deletes cannot sync: PieFed records "delete by hand on Instagram" with a link.

Neither option can edit a caption.

## 14. P2 Bluesky (2026-11-13)

`app/connections/bluesky.py`, registered by `app/user/connections.py`, which also serves the client's documents at
`/connections/bluesky/client-metadata.json` and `/connections/bluesky/jwks.json` (main domain, 404 when Bluesky is
off). `flask connections-bluesky-key` prints a new `BLUESKY_OAUTH_KEY`.

- **Connecting**: the Connect button has a box for a handle, a DID or a server; empty means `BLUESKY_DEFAULT_SERVER`
  (bsky.social). A handle resolves by its `/.well-known/atproto-did`, else by `BLUESKY_RESOLVER`'s resolveHandle
  (which reads DNS; PieFed has no DNS library); a did:plc by `BLUESKY_PLC_URL`. A name no one answers for as a handle
  is tried as a server. After the code exchange, the account's own DID document must name a PDS whose authorization
  server is the one the member signed in at, and a handle-started sign-in must come back as that handle's DID.
- **Keys**: each connection gets its own DPoP key, sealed with its tokens; PieFed's client assertions are signed with
  `BLUESKY_OAUTH_KEY` (kid = its thumbprint).
- **Text**: the Markdown body (links written out, so they become facets), after the title unless the body starts
  with it. The 300 limit is counted in code points, which are never fewer than graphemes, and a cut never ends
  inside a combining sequence; the 3000-byte limit cannot be reached in 300 code points.
- **Pictures**: re-encoded as JPEG under 950,000 bytes and 2000 px, uploaded as blobs with their alt text and aspect
  ratio; a picture that cannot be read is left out and the link said instead.
- **Tests**: `tests/test_connections_bluesky.py` (respx as PLC, resolver and PDS); `interop/run.sh bluesky` runs
  `interop/bluesky/test_bluesky.py`: the harness PieFed connects alice by OAuth on the harness PDS, a microblog's copy
  appears with its hashtag facet, the post's deletion removes it, and disconnecting forgets the account. The harness
  PDS is patched (`interop/pds_harness.sh`) to accept the `.test` client id; `interop/bluesky/piefed_state.py` makes
  piefed.test public and Bluesky on for the run only.

## 15. P3 Facebook Pages (2026-11-14)

`app/connections/facebook.py`. Text and picture handling shared with Bluesky moved to `app/connections/content.py`
(plain text, the cut, a picture's bytes, a JPEG within a size).

- **Connecting**: Facebook Login for Business (`/dialog/oauth`, scopes `pages_show_list`, `pages_manage_posts`,
  `pages_read_engagement`). The code is swapped for a user token, lengthened (`fb_exchange_token`), and
  `/me/accounts` lists the Pages; only Pages whose tasks include `CREATE_CONTENT` are offered, and a sign-in with
  several asks which (the chooser built in P1). The connection keeps the Page token (no expiry when made from a
  long-lived user token) and the user token, which only the revoke uses.
- **Every Graph call** is a POST with Graph's `method` override, so tokens, the app secret and `appsecret_proof` stay
  in request bodies, out of any URL. `FACEBOOK_GRAPH_VERSION` (default `v27.0`) names the API version.
- **Failures** by Graph's error code, not its status (Graph answers nearly everything with 400): 190, 102, 10,
  200, 210, 230 mean reconnect; 1, 2, 4, 17, 32, 341, 368, 613, 80001 and `is_transient` mean try later; the rest
  are refusals, with Facebook's message shown to the author.
- **Posts**: message = text and the link back (cut under 60,000 characters); a link post passes its link for the
  card; one picture is a photo post (`/photos`, JPEG under 4 MB, 4096 px); a gallery uploads up to ten photos
  unpublished and attaches them to one `/feed` post; an uploaded video or a content warning goes as text. NSFW and
  NSFL posts are skipped (§5). Edits replace the message; deletes delete.
- **Tests**: `tests/test_connections_facebook.py` (respx as the Graph API). No end-to-end harness exists for
  Facebook; the owner's live check with a reviewed Meta app is the end-to-end test.

## 16. P4 Instagram (2026-11-14)

`app/connections/instagram.py`, one service with the two variants of C6, offered as two Connect buttons when both
apps' credentials are set ("Instagram, through Facebook" and "Instagram").

- **Through Facebook** (`facebook_login`): the Facebook app's dialog with `instagram_basic`,
  `instagram_content_publish`, `instagram_manage_contents`, `pages_show_list`, `pages_read_engagement`; the Pages'
  linked `instagram_business_account`s are offered. Calls go to graph.facebook.com exactly as Facebook's do, with the
  Page's token (no expiry). Deletes sync. The P0 docs name a user token for these calls; that a Page token serves
  them too is the live check's first question.
- **Directly** (`instagram_login`): `www.instagram.com/oauth/authorize` with `instagram_business_basic` and
  `instagram_business_content_publish`, the code swapped at api.instagram.com, lengthened with `ig_exchange_token`
  (60 days) and renewed with `ig_refresh_token` once a week or less is left. Calls go to graph.instagram.com with the
  token as a bearer header. Meta documents the exchange and the renewal only as GETs with the secret or token in the
  query: those two go to Meta over TLS as documented, and PieFed logs no URLs. A copy cannot be deleted: its row
  becomes `manual`, "Delete it by hand on Instagram".
- **Pictures**: `/connections/media/<token>.jpg` on the main domain, signed (itsdangerous, SECRET_KEY) to one file
  for an hour; it answers a JPEG padded white (never cropped) into 4:5 .. 1.91:1, at most 1440 px and 8 MB, or 404.
- **Posts**: one picture is an image container, two to ten a carousel of item containers; then `media_publish`,
  and the permalink is read (a failure there leaves the copy sent without a link). Caption: text, the post's tags as
  up to 30 hashtags, "(+N more pictures)" past ten, and the link back, cut at 2200. Alt text goes in `alt_text`.
  Only image and gallery posts are offered; NSFW and NSFL posts are skipped. No edits (§2).
- **Errors**: Facebook's code mapping, shared as `answer_of`, plus 9 and 9007 (media not ready) as try later.
- **Tests**: `tests/test_connections_instagram.py`. The owner's live check is the end-to-end test.

## 17. P5 YouTube (2026-11-14)

`app/connections/youtube.py`.

- **Connecting**: Google OAuth with PKCE, `access_type=offline` and `prompt=consent` (so a refresh token always
  comes back), scope `https://www.googleapis.com/auth/youtube`. §3 named `youtube.upload`, but videos.update and
  videos.delete need `youtube` (C4 syncs both), and it covers uploads too. The connection is the member's own
  channel (`channels?mine=true`); an account with none connects nothing. Access tokens last an hour and are
  refreshed; `invalid_grant` means reconnect; disconnect revokes the refresh token.
- **Uploading**: a resumable session (`uploadType=resumable`, metadata first), then one PUT of the whole stored
  original, streamed from this disk (`media_local_path`) or first fetched from S3 into a temporary file (at most
  videos.insert's 256 GB). A failed upload is begun again by the sweep; resuming part way is left out. The PUT has a
  deadline of 600 s plus 1 s per 500 KB.
- **Metadata**: title = the post's title, or its first line, or "Video", within 100 characters; description = the
  text and the link back, cut to stay within 5000 bytes; tags = the post's tags within 500 characters; category 22;
  public; not made for kids. `<` and `>`, which YouTube refuses, become `‹` and `›`. The poster frame is sent to
  `thumbnails.set`; a refusal there (a channel not allowed custom thumbnails) never fails the copy.
- **The daily allowance** (§8): `YOUTUBE_DAILY_UPLOADS` (default 100; 0 stops uploads) counted in Redis by the
  Pacific date before each upload; past it, and on YouTube's own `quotaExceeded`, `dailyLimitExceeded`,
  `uploadLimitExceeded` or `rateLimitExceeded`, the copy waits (QuotaWait) until 00:01 Pacific.
- **Edits** replace title, description and tags (videos.update, part=snippet); **deletes** delete.
- **Tests**: `tests/test_connections_youtube.py`. The owner's live check is the end-to-end test.

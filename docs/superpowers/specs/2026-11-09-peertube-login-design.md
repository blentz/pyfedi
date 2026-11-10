# PeerTube login and account API: PeerTube's app on PieFed

Date: 2026-11-09
Status: owner rulings P1-P5 given 2026-11-09 (§6), each as recommended; part of the API-completeness plan
(`docs/superpowers/plans/2026-11-07-api-completeness.md`, workstream W2.2). Builds on W2.1
(`2026-11-09-peertube-read-api-design.md`). Nothing here is built yet.

## 1. Goal

A person adds a PieFed domain in PeerTube's own Android app (Framasoft's), logs in with their PieFed username and
password, and uses their account:
- they see their subscriptions' videos and their notifications;
- they subscribe to a channel (a community, V2) and unsubscribe;
- they like or dislike a video;
- they comment on a video and reply to a comment.

The proof is the harness:
- the PeerTube app becomes a driven Android app, no longer a protocol probe;
- its `login`, `view` and `post` rows go to `passed`, where `post` means a comment, as the Lemmy apps' rows do;
- uploading is W2.3.

## 2. What the apps call

Read on 2026-11-09 from:
- the official app (Flutter), `framasoft/peertube/mobile-application` v2.3.0, commit 9bd3a4d;
- TubeLab v1.15.0 (2022), the newest public source of it;
- PeerTube's own openapi 8.3.0 and its token controller.

The official app parses strictly. A missing or wrongly typed non-null field throws, and one bad element fails a
whole list. The shapes below are therefore requirements, and contract tests check each one.

- **Adding the instance:** `GET /api/v1/config` must carry `serverVersion` ≥ 7.2.0, which D4 already provides.
  - It also needs about 60 nested fields the read API leaves out, among them `client.*`, `search.*`, `video.*`,
    `user.videoQuota`, `live.*`, `signup.*` and `plugin.registered*`.
  - Each gets the value that says "not offered here": `live.enabled: false`, `import.*: false`, empty plugin lists,
    and so on.
- **Login:**
  - `GET /api/v1/oauth-clients/local` returns `{client_id, client_secret}`.
  - The app then posts `POST /api/v1/users/token` as a form, with `grant_type=password`, the client's id and secret,
    `username` and `password`.
  - The token answer needs `token_type: "Bearer"`, `access_token` and `refresh_token`. `expires_in` is optional:
    without it the app refreshes only on a 401.
  - Errors are recognised by the body's `code`:
    - `invalid_grant` (400) means bad credentials;
    - `missing_two_factor` (401) means ask for a code, sent back in the `x-peertube-otp` header.
- **Refresh:** `grant_type=refresh_token`, with an extra `response_type=code` that is ignored. A 400 with
  `invalid_grant` sends the person back to the login screen.
- **Logout:** `POST /api/v1/users/revoke-token`.
- **The account:** `GET /api/v1/users/me`, whose first read doubles as the token check.
  - It requires `account`, `id`, `username`, `email`, `createdAt` and `lastLoginDate` (never null).
  - It also requires a set of booleans (autoplay, `p2pEnabled`, `blocked`, the `no*Modal` flags), `role`, `theme`,
    `videoChannels[]`, `videoQuota` and `videoQuotaDaily`.
  - TubeLab also needs `notificationSettings`.
- **Account screens:**
  - `users/me/subscriptions` (GET, POST `{uri}`, DELETE `/{handle}`, `exist?uris=`);
  - `users/me/subscriptions/videos`;
  - `users/me/notifications` (`start`, `count`, `unread`; `total` is the unread badge), plus `/read {ids}` and
    `/read-all`;
  - `users/me/history/videos`;
  - `users/me/videos/{id}/rating` and `PUT videos/{id}/rate {rating}`;
  - `POST videos/{id}/comment-threads {text}`, `POST videos/{id}/comments/{cid} {text}` and
    `DELETE videos/{id}/comments/{cid}`. These answer **200** with `{comment: {...}}`.
- **Notification `type`** must be one of PeerTube's 1-22. One unknown value fails the official app's whole list.
- **TubeLab's trap:** it reads `/.well-known/nodeinfo` before logging in. If `software.name` is neither
  `peertube` nor `mastodon`/`pleroma`, the login does nothing and shows no error (`LoginActivity.java:338-351`).
  Only a missing or failing nodeinfo makes it fall back to PeerTube.

## 3. Mapping

- **The local client:**
  - **One OAuth app per domain**, made on first request and kept, marked as PeerTube's local client. Its secret is
    shown to anyone who asks, as PeerTube shows its own, so the secret protects nothing.
  - **The password grant is accepted for that client only (P1).** Every other app keeps PieFed's authorization-code
    flow.
  - The password check is the alpha API's login (`app.shared.auth.api_log_user_in`):
    - the same account finder;
    - the same refusals: pending registration, bans and the onion login gate;
    - the same audit, and the same limit of 20 attempts an hour per address.
  - Tokens are the existing OAuth tokens (`app.oauth_server.tokens`), so revoking them, a password change, a ban and
    the per-domain rule all apply unchanged.
- **Token lifetime (P2):** per ruling M6, they never expire and no `expires_in` is sent. Refresh works and rotates
  the pair.
- **Two-factor:** PieFed has none (the alpha API's TOTP routes are stubs). `missing_two_factor` is never sent, and
  an `x-peertube-otp` header is ignored.
- **Subscriptions:** a subscription to a channel is a community membership (V2), so subscribing and unsubscribing go
  through `app/shared` (`join_community`/`leave_community`), with their federation. Subscription videos are the
  video posts (W2.1's selection) of joined communities, newest first.
  - Following a *person* has no PeerTube equivalent in the app, so it is left out.
- **Rating:** `like` is an upvote and `dislike` a downvote (as M5), `none` takes a vote back. All three go through
  the shared vote code. The rating read is the person's vote.
- **Comments:** through the shared reply code, with its rate limits, spam checks and federation. `text` is Markdown,
  as PeerTube's is. A comment on a remote video federates to that video's community as any reply does.
- **History:** PieFed's `read_posts`, limited to video posts, newest read first. Removing history entries deletes
  read marks (P5). The official app reports watching with `POST videos/{id}/views` (every few seconds while it
  plays): a signed-in viewer's marks the post read, at most once in ten minutes; a signed-out one's is taken and
  counted nowhere, as PieFed counts no views.
- **Notifications (P3):** PieFed's types are mapped to the nearest PeerTube type, and the rest left out of the list
  and its `total`:

  | PieFed type | PeerTube type |
  |---|---|
  | `NOTIF_REPLY`, `NOTIF_POST` (comments) | 2 `NEW_COMMENT_ON_MY_VIDEO`, with its `comment` object |
  | `NOTIF_MENTION` in a comment | 11 `COMMENT_MENTION` |
  | `NOTIF_USER`, `NOTIF_COMMUNITY`, `NOTIF_TOPIC`, `NOTIF_FEED` (new posts) | 1 `NEW_VIDEO_FROM_SUBSCRIPTION` |
  | `NOTIF_FOLLOW` | 10 `NEW_FOLLOW` |

  Types 1, 2 and 11 carry a video, so they are sent only when a video is the subject: a new video post, or a comment
  on one. The rest go unsent, as a mention in a post does. Who may be told of what is the Mastodon API's rule
  (`app.mastodon.notifications.shown`). Reading marks PieFed's own rows read.
- **`users/me`:**
  - `videoChannels` lists the communities the person owns. Upload targets them in W2.3.
  - `email` is the person's own, for a token from the PeerTube local client only. Another app's token, such as a
    Mastodon app's from its own consent screen, gets an empty one (security review 2026-11-09).
  - `lastLoginDate` is their `last_seen`.
  - `role` is 0 Administrator for an admin, else 2 User. PieFed's custom roles have no PeerTube equivalent; the app
    shows admin screens only to PeerTube's moderators and admins.
  - Quotas are -1 (unlimited) until W2.3 sets a real one.
  - Every boolean is the PeerTube default unless PieFed has the same setting: `autoPlayVideo` maps to nothing, so it
    is `true`.
  - `PUT users/me` changes `displayName` and `description` only. Other fields are accepted and ignored, as PeerTube
    ignores unknown ones.
- **Reads:** every read route takes the app's token when it sends one and reads as that person. Their blocks and
  filters apply, and a private community they belong to can be opened, though it is never listed as a channel. With
  no token, a route reads as a signed-out reader, and a private domain refuses with 401.
- **Errors:** PeerTube's `application/problem+json` shape, `{type, title, detail, status, code}`, on every
  `/api/v1` route this phase adds, and on 401 for a missing or bad token.

## 4. Where it lives

- `app/peertube/auth.py`: oauth-clients/local, the token endpoint (the password grant registered on the existing
  Authlib server, restricted to the local client), and revoke-token.
- `app/peertube/me.py`: users/me, subscriptions, history, notifications and ratings.
- Comments go into `videos.py` beside the comment reads.
- A `token_required` decorator reads the Bearer token through `app.oauth_server.protect`. The per-domain login gate
  lists these routes, so a private domain still asks for a login first.

## 5. Phases (M8)

- **W2.2a, the server:**
  - everything above, with unit tests at 100% line and branch;
  - contract tests of every entity against the official app's required fields;
  - the probe's `login` passes.
- **W2.2b, the harness:**
  - a PeerTube-app driver on the emulator: add `tube.test` (public, so the app shows videos signed out too), log in
    as tess, open the seeded video, comment with the run's marker, and check the comment through PieFed's API;
  - the matrix's PeerTube row moves from probe to driven.
  - **TubeLab (P4):** it stays a probe, expected `login: unsupported`, with the nodeinfo reason recorded.

## 6. Owner rulings

**Ruled 2026-11-09:**
- P1 (a): the password grant, for the local client only.
- P2 (a): tokens never expire and stay revocable.
- P3: the four rows of §3's notification table only.
- P4 (a): TubeLab is left out.
- P5: history is read marks.

The questions as asked:


- **P1. The password grant.** PeerTube's apps log in only by sending the password to the token endpoint (OAuth's
  password grant, which OAuth 2.1 drops and Mastodon removed). Options:
  - (a) accept it for the PeerTube local client only, with the alpha login's checks and rate limit;
  - (b) refuse it, and leave PeerTube's apps signed out.

  The alpha API already takes passwords from apps (`/api/alpha/user/login`), so (a) adds no new exposure.
  **Recommendation: (a).**
- **P2. Token lifetime.** (a) Never expiring, revocable, as M6 rules for Mastodon; (b) PeerTube's own (1 day access,
  2 weeks refresh). Both work with the official app: without `expires_in` it refreshes on a 401. TubeLab refreshes on
  every start either way. **Recommendation: (a)**, one rule for every app.
- **P3. Notifications.** Map only the four rows of §3's table and leave the rest out (chat messages, bans, moderation,
  reports, reminders), or map them all to the nearest type. A wrong type's text in the app would mislead, such as a
  ban shown as "new follower". **Recommendation: the four rows only.** People see the rest on the web.
- **P4. TubeLab.** Its login stops unless nodeinfo says `peertube`. Options:
  - (a) leave TubeLab out (its newest public source is from 2022), recorded in the matrix;
  - (b) answer `peertube` in nodeinfo to TubeLab by user agent, as D3 does for Jerboa. TubeLab sends okhttp's default
    agent, though, which many apps share, so this would mislead others.

  **Recommendation: (a).**
- **P5. History.** Make `users/me/history/videos` PieFed's read marks of video posts, and have deleting history
  delete read marks, which also changes what "hide read posts" hides on the web. Or answer an empty list and accept
  the deletes. **Recommendation: read marks.** It is the same fact, so the web and the app agree.

## 7. Security and privacy

- The password grant takes a password only from the local client of the domain asked, over that domain. It has the
  alpha login's limit (20 an hour per address), its timing-safe refusal of unknown names, and the onion login gate.
  A refusal is `invalid_grant` whatever the reason, so it never tells which names exist.
- The local client's secret is public by design, as PeerTube's is: the password is the credential. The refresh grant
  still needs that secret, as the existing server requires, so a leaked refresh token used with it works only on that
  domain.
- Tokens are hashed at rest and per domain (ruling 2026-11-08). Logging out revokes the pair.
- Every read is the person's view, with their blocks, filters, NSFW/NSFL settings and the domain's visibility rules,
  as on the web.
- No password, token or OTP header is logged.

## 8. Out of scope

- Playlists (watch later), video uploads (W2.3), avatar changes, registration through the app, password reset from
  the app (the app is sent to the web page), notification settings beyond reading them, blocklists, and OpenID login.

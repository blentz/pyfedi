# Lemmy `/api/v3` for Jerboa

Date: 2026-11-10
Status: owner rulings L1-L4 given 2026-11-10 (§6), each as recommended; built 2026-11-10 on `feat/lemmy-v3` as
described below. Part of the API-completeness plan
(`docs/superpowers/plans/2026-11-07-api-completeness.md`, workstream W3), which ruled D3 on 2026-11-07: a v3 shim on
the main host, with nodeinfo answering Jerboa by name.

## 1. Goal

A person adds a PieFed domain in Jerboa (Lemmy's own Android app), logs in with their PieFed username and password,
and uses their account as on a Lemmy instance:
- they browse their subscribed, local and all feeds, open a post and read its comments;
- they vote, comment and reply, save, and subscribe to a community;
- they see replies and mentions in their inbox.

The proof is the harness:
- Jerboa becomes a driven Android app instead of a protocol probe;
- its `login`, `view` and `post` rows go to `passed`, as the other Lemmy apps' rows do.

## 2. What Jerboa calls

Read on 2026-11-10 from:
- Jerboa 0.0.88 (`LemmyNet/jerboa`, commit e41bbda), the F-Droid build's version;
- the API library it pins, `it.vercruysse.lemmyapi:lemmy-api` 0.4.2-v1patch (`MV-GH/LemmyBackwardsCompatibleAPI`, tag
  `0.4.2-v1patch`, commit 7402832).

The library decodes with kotlinx.serialization and `ignoreUnknownKeys`. A property that is neither nullable nor
defaulted is therefore required, and so is a nullable one without a default. A missing one throws, and the whole answer
fails, a list included. The models are read from the library's source into `tests/fixtures/jerboa_models.json` by
`interop/apps/kotlin_models.py`, and contract tests check every answer against them, as the PeerTube app's are
checked.

- **Identity:**
  - Jerboa reads `GET /nodeinfo/2.0.json` through its API library's own client, whose `User-Agent` is `LemmyKotlinApi`
    (`HttpClient.platform.kt:15`); its other requests send `User-Agent: Jerboa` (`api/Http.kt:62,79-81`). Found on the
    emulator: D3 assumed `Jerboa` on every request.
  - It refuses the instance unless `software.name` is `lemmy`.
  - It picks the library's wrapper from `software.version`: 0.19.11 and later use the newest 0.19 models.
- **Login:**
  - `POST /api/v3/user/login` with `{username_or_email, password, totp_2fa_token}`.
  - The answer is `{jwt, registration_created, verify_email_sent}`.
  - Every later call sends `Authorization: Bearer <jwt>`, except an image upload, which sends it as the `jwt`
    cookie.
  - A request body carries every optional field it was not given as `null` (kotlinx's `explicitNulls`); in Lemmy that
    means not given.
- **After login:**
  - `GET /api/v3/site` must carry `my_user`, which Jerboa reads without a null check (`LoginViewModel.kt`).
  - Its `local_user_view.local_user` carries the default sort and listing.
  - `moderates` being non-empty makes Jerboa also call `GET /api/v3/user/report_count`; every start checks the token with `GET /api/v3/user/validate_auth`.
  - An admin also calls `GET /api/v3/admin/registration_application/count`.
  - `GET /api/v3/user/unread_count` feeds the inbox badge.
- **Errors:** a 4xx with `Content-Type: application/json` and `{"error": "<code>"}`. Jerboa names several codes on
  its login screen, among them `incorrect_login`, `email_not_verified`, `registration_application_is_pending`,
  `missing_totp_token` and `incorrect_totp_token`. Any other code is shown as it is.

Endpoints, and what each answer is decoded into:

| Endpoint | Jerboa's use | Answer |
|---|---|---|
| `GET site` | start, login | GetSiteResponse |
| `POST user/login` | login | LoginResponse |
| `GET post/list` (`type_`, `sort`, `page`, `limit`, `community_id`/`community_name`, `saved_only`, `page_cursor`, `show_hidden`, `show_read`) | feeds | GetPostsResponse (`posts`, `next_page`) |
| `GET post` (`id`, `comment_id`) | post screen | GetPostResponse |
| `GET comment/list` (`post_id`, `parent_id`, `sort`, `max_depth`, `type_`, `page`, `limit`) | post screen | GetCommentsResponse |
| `POST comment`, `PUT comment`, `POST comment/delete` | comment | CommentResponse |
| `POST post/like`, `POST comment/like` (`score` -1/0/1) | vote | PostResponse, CommentResponse |
| `PUT post/save`, `PUT comment/save` | save | PostResponse, CommentResponse |
| `POST post/mark_as_read`, `POST post/hide` | reading | SuccessResponse |
| `POST post`, `PUT post`, `POST post/delete` | posting | PostResponse |
| `GET community` (`id`/`name`), `POST community/follow`, `POST community/block` | community | GetCommunityResponse, CommunityResponse, BlockCommunityResponse |
| `GET user` (`person_id`/`username`, `sort`, `page`, `limit`, `saved_only`) | profile | GetPersonDetailsResponse |
| `POST user/block` | blocks | BlockPersonResponse |
| `GET user/unread_count`, `GET user/replies`, `GET user/mention`, `GET private_message/list` | inbox | GetUnreadCountResponse, GetRepliesResponse, GetPersonMentionsResponse, PrivateMessagesResponse |
| `POST comment/mark_as_read`, `POST user/mention/mark_as_read`, `POST private_message/mark_as_read`, `POST user/mark_all_as_read` | inbox | the view marked, or GetRepliesResponse |
| `POST private_message` | inbox | PrivateMessageResponse |
| `GET search` (`q`, `type_`, `sort`, `listing_type`, `community_id`, `page`, `limit`) | search | SearchResponse |
| `POST /pictrs/image` (`images[]`, the token as the `jwt` cookie), then `GET /pictrs/image/<file>` | image upload | UploadImageResponse (`{msg, files: [{file, delete_token}]}`, pict-rs's shape); the library makes each URL from `file` |
| `POST post/report`, `POST comment/report` (`reason`) | reporting | PostReportResponse, CommentReportResponse |
| `GET post/site_metadata` | posting a link | GetSiteMetadataResponse |
| `PUT user/save_user_settings` | settings | SuccessResponse |
| `GET user/report_count`, `GET admin/registration_application/count`, `GET user/validate_auth` | a moderator's and an admin's badges, on every start | GetReportCountResponse, GetUnreadRegistrationApplicationCountResponse |
| moderation (`post/report/list`, `comment/report/list`, `post/lock`, `post/feature`, `post/remove`, `comment/remove`, `comment/distinguish`, `community/ban_user`, `post/like/list`, `comment/like/list`) | a moderator's tools | per §6 L2 |
| admin (`admin/registration_application/*`, `user/ban`) | an admin's tools | per §6 L2 |

## 3. How it is built

`/api/alpha` already serves the same paths with Lemmy's type names: PieFed's API began as Lemmy's. W3 is an adapter
over it, a blueprint `app/lemmy/` whose handlers:
1. rename the request (`username_or_email` → `username`, and so on), then call alpha's own handler functions
   (`app/api/alpha/utils/*`), so visibility, blocks, bans, NSFW preferences, per-domain login, rate limits and every
   write go through the code alpha uses (plan §3);
2. rename and fill the answer to Lemmy 0.19.11's models. The full gap was measured from the generated contract against
   alpha's schemas (`app/api/alpha/schema.py`):
   - renamed: `Post.title`→`name`, `Post.user_id`→`creator_id`, `Post.sticky`→`featured_community`,
     `Post.instance_sticky`→`featured_local`; `Comment.body`→`content`, `Comment.user_id`→`creator_id`;
     `Person.user_name`→`name`, `Person.bot`→`bot_account`; the answers' wrappers (`posts`, `comments`, `post_view`,
     `comment_view`, `community_view`, `person_view`);
   - filled from PieFed's own data: community and site aggregates (`subscribers`, `posts`, `comments`,
     `users_active_*`), `creator_blocked`, `banned_from_community`, `posting_restricted_to_mods`, `visibility`;
     LocalUser's settings (`show_nsfw`, `default_sort_type`, `default_listing_type`, `show_read_posts`, `admin`, ...);
   - fixed values where PieFed has no such thing: `taglines: []`, `blocked_urls: []`,
     `CommentResponse.recipient_ids: []`, `local_user.last_donation_notification` the present moment (Jerboa's F-Droid
     build shows Lemmy's donation dialog on every start while it is a year old, and PieFed cannot mark it shown);
   - a null in a request body is left out before alpha's schema loads it, so it is neither refused nor taken as
     "clear this";
3. map errors to `{"error": code}`: alpha's own handler decides what is logged and what is an internal error, and its
   message is the code, with a 400; 404 `not_found` for a missing object; 429 `rate_limit_error`; 401
   `incorrect_login` for a token that is no longer good. The handler is registered by status code too, since Flask
   finds the app's own 404 and 429 handlers before a blueprint's handler by class. An endpoint W3 does not serve
   answers 404 `{"error": "not_found"}`, never a 500 (plan §3).

The site document: `GET /api/v3/site` is the public Lemmy-shaped document other servers and crawlers have long read
(`app.activitypub.routes.lemmy_site`, cached per domain; it never says registration is open, against spam crawlers).
It is made complete for Lemmy 0.19.11's model (a custom emoji's id, alt text and date among it), and sent a token it
also carries `my_user` and is then never cached. Its `version` stays PieFed's: Jerboa reads its version from nodeinfo.

Limits: where alpha limits a write on its route (making a post or a comment, sending a private message, uploading an
image, reading a link's metadata), both APIs draw on one bucket per address (`limiter.shared_limit`: `limit()` keys
by endpoint as well as scope). The login has a 20/hour bucket of its own, as each API's password login has.

Images: each one posted to `/pictrs/image` is stored by alpha's upload, and its `file` is the stored URL signed with
the site's key. `GET /pictrs/image/<file>` redirects to that image, and only to an image this server signed, so it is
no open redirect. A post whose link is one of these is stored with the image's own URL. The upload checks its own
token (the cookie), so it works on a private instance.

Sorts: Lemmy's names map to PieFed's (`Hot`, `New`, `Old`, `Active`, `Scaled`, `Controversial`, `MostComments`,
`NewComments`, `Top*`). One PieFed has no match for takes its nearest, listed in the adapter and tested one by one.
`ListingType` `ModeratorView` is alpha's `ModeratorView`. A thread asked for by depth with no limit, as Jerboa asks,
is read from alpha a page at a time up to Lemmy's own 300 comments. Settings alpha keeps no field for (the default
listing) are set on the person directly; ones PieFed has no such thing for (a theme, keyboard navigation) are not
kept.

Identity (D3): `/nodeinfo/2.0(.json)` and `/nodeinfo/2.1(.json)` answer `software: {name: lemmy, version: 0.19.11}`
to a User-Agent beginning `Jerboa` or `LemmyKotlinApi` and `piefed` to every other agent. The cache key gains the agent's class and the
answer sends `Vary: User-Agent`, so one is never served to the other. This is the API level served, not PieFed's
version.

The JWT is alpha's own: the same token works in both APIs, and logging out of one logs out of the other.

## 4. What PieFed has that Lemmy does not

Jerboa shows Lemmy's post types only. Per §6 L3:
- a poll is a text post whose body ends with its options and their counts, and a link to vote on the web;
- a gallery is an image post of its first image, with the rest as images in its body (Markdown, which Jerboa shows);
- an event is a text post whose body starts with its time and place;
- flairs and emoji reactions are left out; a reply's emoji-only reactions are not shown;
- a feed (a PieFed collection of communities) and a topic are not listed: Jerboa has no screen for them.

## 5. Harness

- Jerboa (`com.jerboa`, F-Droid) is a driven app: `login` logs in as alice on piefed.test from its drawer, `view`
  finds the seeded post in her home feed (its search finds communities only), `post` comments on it and PieFed has
  the comment.
- On a fresh install Jerboa asks its default instance, lemmy.ml, for nodeinfo before anything else and nothing catches
  that failing: offline, the timeout killed the app. The emulator's network has a lemmy.ml stand-in (Caddy) that
  answers nodeinfo as Lemmy and 404 to the rest.
- The `lemmy-v3` probe stays for apps that are not driven, now expecting `passed`; it reads Lemmy's own login
  refusal (`{"error": "incorrect_login"}`) as served.

## 6. Decisions for the owner

- **L1. The Lemmy version reported.** 0.19.11, the newest the library knows: its newest models, which need a few
  more fields (`hidden`, `unread_comments`, `alt_text`, the vote display mode), and every feature Jerboa gates on
  version (hiding posts needs 0.19.4). Or 0.19.3, fewer fields and fewer features. Recommendation: 0.19.11.
  **Ruled 2026-11-10: 0.19.11.**
- **L2. Moderation and admin tools.** (a) W3 serves a member's endpoints only; a moderator's and an admin's answer
  `not_found`, `report_count` answers zeros so the badge never errs, and moderating stays on the web or in a PieFed
  app. (b) Also serve moderation over alpha's handlers (reports, lock, feature, remove, ban from community, vote
  lists) as a second phase, W3b. Recommendation: (a) now and (b) as W3b, after the member's endpoints pass in the
  harness.
  **Ruled 2026-11-10: (a) now, (b) as W3b.**
- **L3. PieFed-only posts.** Show polls, galleries and events as the nearest Lemmy post with the rest in the body
  (§4), or leave them out of Jerboa's lists. Recommendation: show them; leaving them out hides real posts.
  **Ruled 2026-11-10: show them.**
- **L4. Who `/api/v3` answers.** Every caller (the path is Lemmy's, used by no PieFed client, and Lemmy tools such as
  bots can use it too), or Jerboa's User-Agent only. Nodeinfo still says `lemmy` to Jerboa alone (D3).
  Recommendation: every caller.
  **Ruled 2026-11-10: every caller.**

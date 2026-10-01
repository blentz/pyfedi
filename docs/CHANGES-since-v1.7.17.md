# Changes since v1.7.17

This document summarises what changed on the `blentz` branch between the tag
`v1.7.17` and the current HEAD. It is written for two audiences: instance
operators who are upgrading, and PieFed developers reviewing the fork. The
upgrade notes come first because they are what an operator must act on.

Short commit hashes (8 characters) are given inline so that any claim here can
be traced to the commit that made it. Defect identifiers such as `D895`,
`R207` or `PERM-1` refer to rows in the findings register,
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`.

---

## 1. Overview

This body of work began as a test-coverage campaign. Writing tests against
code that had never been executed under test surfaced roughly 1,400 registered
defects, each recorded in the findings register with the evidence that
reproduced it. The campaign was followed by two days (2026-09-30 and
2026-10-01) in which the open register was worked through: every open defect
was fixed, closed as not a defect, or deferred, one commit per defect, each
with a failing test written first. Where a fix required a choice about how
PieFed should behave, that choice was put to the project owner and the owner's
ruling was implemented; none of the behaviour changes below were decided by
the implementer alone.

The range contains 2,431 commits:

| Type | Commits |
|---|---|
| `test` | 985 |
| `fix` | 699 |
| `docs` | 638 |
| `refactor` | 36 |
| `chore` | 32 |
| `security` | 12 |
| `feat` | 12 |
| `perf` | 2 |
| other (`revert`, untyped) | 15 |

The test suite now has 17,142 passing tests.

---

## 2. Upgrade notes

### 2.1 Database migrations

Eight Alembic migrations were added. They form a single chain on top of
`7b8bf43fa079`, which is already present in v1.7.17, so `flask db upgrade`
applies them in the order below. Several of them rewrite data; none delete
rows.

The NOT NULL migrations use a lock-friendly recipe: each adds a
`CHECK (col IS NOT NULL) NOT VALID` constraint, validates it (which scans the
table but only takes a `SHARE UPDATE EXCLUSIVE` lock, so reads and writes
continue), then sets NOT NULL without a second scan and drops the constraint.
This needs PostgreSQL 12 or later to avoid a full-table `ACCESS EXCLUSIVE`
scan. The backfill `UPDATE`s touch only rows that are actually NULL.

| Order | Revision | What it does | Rewrites data? |
|---|---|---|---|
| 1 | `9e99070afe06` backfill_reply_count_cross_posted | Fills `post.reply_count_cross_posted` from `reply_count` where it is NULL, and gives the column a server default of 0. Several code paths did `None - 1` on old rows. (`331f47ed`) | Yes, NULL rows only |
| 2 | `c4f1a9d7e2b8` post_sort_columns_not_null | Backfills and makes NOT NULL the seven columns the post sorts page by: `sticky`, `instance_sticky`, `score`, `ranking`, `ranking_scaled`, `posted_at`, `last_active`. `posted_at` falls back to `created_at`; `last_active` falls back to the newest reply's time, then `posted_at`. Adds matching server defaults. A NULL in any of these made a post silently missing from every page of that sort. (`097b8a65`) | Yes, NULL rows only. This is the largest migration: it scans `post` once per column |
| 3 | `1812a0b161d5` user_verified_not_null | Sets `user.verified = false` where NULL, adds a server default, and makes the column NOT NULL. A NULL user had been let through guards as if verified (D645). (`054b2a0c`) | Yes, NULL rows only |
| 4 | `05ae68b2ebb9` user_unread_notifications_not_null | Sets `user.unread_notifications = 0` where NULL, adds a server default, makes it NOT NULL. Incrementing a NULL count raised `TypeError` (D274). (`32b8716b`) | Yes, NULL rows only |
| 5 | `9c3e5a1d7b20` passkey_counter_reset | Sets every `passkey.counter` to 0. The stored values were invented by the old code rather than reported by authenticators; real counts are recorded from each passkey's next login (D886, D887). (`19ce897d`) | Yes, every passkey row. Not reversible |
| 6 | `3f8b2c6d9e41` event_more_info_url | Adds a nullable `event.more_info_url` column (R223). (`0079d346`) | No |
| 7 | `23d65cdb8207` pending_follows_are_null | In `user_follower`, outward follows stored as `is_accepted = false` that still have a matching follow request are reset to NULL (pending). Rows with no request left are genuine refusals and stay `false` (R265). (`4e99e002`) | Yes, matching rows only |
| 8 | `8c1d4e7f2a90` quote_authorization | Creates the `quote_authorization` table (post, reply, quoting URI, approval time), used by `/quote_boost_auth` (R205). (`8e83df63`) | No |

Downgrades remove defaults, constraints, columns and the new table, but do not
restore the NULLs or invented counters that the upgrades replaced; there is no
record of which rows held them.

### 2.2 Configuration

New or changed settings in `env.sample`, `env.docker.sample` and `config.py`:

| Setting | Default | Effect |
|---|---|---|
| `TRUSTED_CLIENT_IP_HEADER` | empty | Where the client IP is read from for rate limiting, IP bans, the honeypot ban, geolocation and the `user.ip_address` audit column. Empty means `request.remote_addr`, which `ProxyFix(x_for=1)` already takes from the last `X-Forwarded-For` entry your proxy appended. **Behind a single Caddy or nginx proxy, leave it empty.** Behind Cloudflare or a similar CDN, set it to the CDN's header (for example `CF-Connecting-IP`), and only if clients cannot reach PieFed directly. If the header holds a list, the last entry is used. Previously PieFed read `CF-Connecting-IP` and then the *first* `X-Forwarded-For` entry, which the client controls. (`9c215e2a`) |
| `WEBHOOK_SECRET` | empty | Shared secret for the plugin `/webhook` endpoint. Empty disables the endpoint (it answers 404). When set, a request must carry it in an `X-Webhook-Secret` header (compared in constant time) or it is refused with 403. **If you use a webhook plugin, set this and configure the sender, or the plugin stops receiving calls.** `docs/PLUGINS.md` is updated to match. (R223, `298301cb`) |
| `MAX_CONTENT_LENGTH` | `104857600` (100 MB) | Largest request body accepted, in bytes. Larger requests are refused with 413 before the body is read (a rendered page on the web, JSON under `/api/`). Image uploads are separately capped at 10 MB, so raise this only if you allow large video uploads. (`c2a8c817`) |
| `SKIP_RATE_LIMIT_IPS` | `127.0.0.1` | Parsing changed. It is now always a list: a comma-separated string from the environment is split and trimmed. Previously, once set from the environment it was a plain string and the membership test was a *substring* test, so `192.168.1.10` also exempted `92.168.1.1` and `2.168.1.1` from the login rate limits (D1396, `d7ca6b51`). Existing comma-separated values keep working. |
| `redirect_policy` (admin setting, not `.env`) | `same_origin` | Stored in the `Setting` table and chosen on the admin Misc page. Controls which hosts `?next=` and `Referer` redirects may go to: `same_origin` (this server only), `trusted_servers`, `federated_servers` (known, not banned, not gone) or `all_referrers` (labelled dangerous). The default keeps the new strict behaviour. URL parsing rules do not change with the policy. (`13fe5d50`) |

`config.py` no longer imports `app.constants`; it reads `VERSION` from
`app/constants.py` by path so that `import config` does not cycle through
`app/__init__.py` (`23dfdc71`). No action is needed.

Packaging: `pytest` was removed from `requirements.txt`. Test-only
dependencies (pytest, pytest-cov, pytest-timeout, pytest-xdist, respx, moto,
fakeredis, atheris) live in the new `requirements-test.txt`, installed only by
a new `test` stage in the `Dockerfile`, which sits above `runtime` so the
default build is still the production image (`15fbf52a`). atheris has no
aarch64 wheel, which is why it must stay out of the production install.

### 2.3 Behaviour changes operators and users will notice

**State-changing links became POST forms.** Actions that changed state on a
GET (and so could be triggered by any page a signed-in user visited) are now
POST-only with a CSRF token. The buttons look the same; each is now a small
form, and the htmx variants post the same form.

- Joining a community, `/<actor>/subscribe` (`af6729e6`); leaving one,
  `/<actor>/unsubscribe`, and join-then-post, `/<actor>/join_then_add`
  (`ec266984`). The feed and topic post pickers reach `join_then_add` with a
  307 so the browser re-posts its own form.
- Adding a community to a feed (`44305e92`), joining a feed (`11de66ff`),
  leaving a feed (`a89e5adf`).
- The notification bells: feed (`cdbaef11`), post and comment
  (`b897c175`), profile (`ba1429e9`).
- The feed dropdown's dead "None" item (a link to the removed
  `/feed/remove_community`) is gone.

**Invite and remote-interaction links land on the community page.** Because
joining is now a POST, the FEP-3b86 intent `/c/<actor>/subscribe` (the link
used in community invites) and `/activitypub/externalInteraction` no longer
join directly; they open the community page, where the reader confirms with
Join (`af6729e6`).

**Passwords.** Leading and trailing whitespace is stripped from a password
wherever it is set or checked: registration, login, settings, the reset link,
the admin edit, the API and NNTP. A password that was stored with edge
whitespace by a path that did not strip it will stop matching; the owner
accepted this, and affected users need a password reset (`9d77ebf3`).
A password reset by any route (forgot-password, admin, CLI) now revokes the
user's API tokens, as the settings-page change already did (`8888ab77`).
`lemmy-import` now stamps `password_updated_at` too (`2b86aa1b`).

**Passkeys.** Login now checks the authenticator's signature counter and
stores the real one; the migration resets stored counters to 0 so that no
existing passkey is refused (`19ce897d`). An unknown username gets passkey
options indistinguishable from a real account's (`8c859131`), and an account
with no passkeys fails verification like any other failure (`7ed637ca`).

**Login and accounts.** Web and API login share one finder that matches email
case-insensitively, so both accept the same forms (`0d5ba979`). Disconnecting
an account's last login method is refused with a message to set a password
first (`fe03785b`). A registration that fills the honeypot field is refused,
and the field is marked up so autofill skips it (`487f3b62`).

**`resolve_object` requires authentication.** `GET /api/alpha/resolve_object`
used to let an anonymous caller make the instance fetch and store remote
content. It now answers the standard `incorrect_login` 400 without a token
(PERM-1, `390b958f`).

**Instance-banned users are refused in more places.** One helper now decides
whether a user is banned from a community, counting both community and
instance bans and a fresh ban row. Join-then-post, onboarding's topic join,
the feed and topic post pickers, the inbound Follow, profile-import joins, the
import tasks and the API follow all use it (D995, `e5fc1027`).

**Moderation scope is one rule.** Every post, reply and community moderation
gate now asks one predicate, `can_moderate`: a moderator of the community, an
admin, staff, or a holder of the "administer all communities" permission
(`2ebd72ce`). Consequences:

- Staff may now edit a community and read its subscriber list.
- A site admin may restore and lock a reply.
- A *remote* instance's admin (an `InstanceRole` admin) no longer gets local
  moderation powers that some guards used to grant.
- Community delete and restore stay limited to the owner and admins
  (`418d8f62`).
- An author undoing a moderator's removal of their own post gets 403, and an
  "administer all communities" holder may remove a post (`7fa96072`,
  `6a25a43f`).
- Only an admin may ban an admin, and nobody may ban user 1, on web and API
  (`588ffe6f`). User 1's ban exemption is one named helper,
  `User.is_ban_exempt` (`e991e2de`).

**User 1 receives admin notifications.** `Site.admins()` used to drop the
founding account when it had no role row, so it missed notifications sent to
admins. It is now included (`4783cb9e`).

**Private instances.** A private instance serves no sitemap, and a public
instance's sitemap omits posts from private and local-only communities
(R222, `0cd01768`). RSS on a private instance works for a URL carrying a
member's valid RSS token and stays 404 for anonymous readers (R219,
`2862e8bf`); page RSS links include the member's token (`9c552a79`). Earlier
in the campaign, every RSS feed (not just the front page), the embed, oEmbed
and calendar routes, five metadata routes and the modlog were brought behind
the private-instance check (`1a942490`, `3bf673ee`, `968eb02b`, `dfd33f8e`),
and the check now runs before the response cache and conditional-request
handling (`784a4ebe`, `84090c09`).

**Deleted content.** A post its author deleted shows a "deleted by author"
placeholder for title and body, with its comments still visible; moderators
and admins see the original (D1085, `34b96f15`).

**Follows.** A refused follow of a remote user is shown as "Request declined"
with a way to retry; a pending one is stored as NULL rather than `false`
(R265, `4e99e002`).

**Uploads.** Every image upload on the post and event forms is capped at
10 MB (previously only `.gif` was) (R207, `c2a8c817`). `.mov` uploads and links
are treated as video like `.mp4` and `.webm` (D476, `e853811f`). Genuine
`.heic` uploads are accepted (`b7268c99`) and `.heif`-named files are accepted
and decoded the same way, including for community icons and banners
(`3d8fab48`, `2937b8d8`). The admin site-icon upload no longer accepts `.gif`
(D918, `c9e3983e`). An SVG upload that cannot be sanitised is refused rather
than stored (`fbf39569`).

**Search.** A logged-out search never returns NSFW content, whatever `nsfw=`
says, and the form no longer offers the choice (D798, `baea0c81`).

**Feeds.** The server refuses to rename a feed that has subscribers, including
crafted POSTs and the API (D696, `da09d3a6`). Admins may edit any feed on the
web as the API already allowed (D697, `c5c1d506`). Creating an NSFW or NSFL
feed through the API is refused where the site disallows it (D708,
`27002782`). `feed_auto_leave` defaults to off in the model as in the form
(D663, `da93644b`).

**Reply keyword filters work.** A user's reply filters were never applied.
A matching reply now renders as a collapsed "Filtered: <keyword>" stub with
its replies still visible. Admin and moderation views are unfiltered (R168,
`dbc59344`).

**Scheduled posts.** A scheduled post re-checks `can_create_post` at each
publication and skips (and logs) an occurrence that would be refused (F7,
`2d5d3937`).

**Admin pages.** `/admin/` itself had no permission check and showed host
details to any signed-in account; it now requires admin permission
(`b781a6b6`). Every admin route sends an anonymous visitor to log in
(D943, `2cd432f6`). Masquerading as an account writes an admin-only modlog
entry (D942, `c976e56f`). `/admin/perf_test` runs 1,000,000 iterations by
default with `?n=` clamped to that (R237, `bc94b622`). The LibreTranslate
status on the admin home is fetched with a 3-second timeout and cached for an
hour (D909, `6b5b56d6`). A new admin tool follows accounts from a Mastodon
server's public directory (`7ed73727`).

**Request-path retries removed.** A failed remote actor fetch or feed
webfinger no longer sleeps and retries on the request or inbox thread; only
housekeeping Celery tasks retry (D738, D775, `2c52f5ce`, `58920f33`). Profile
refresh tasks give up on a failed fetch instead of sleeping (D224,
`726c5a7d`).

### 2.4 API contract changes (for client developers)

- **Internal errors are a generic 500.** Any exception that is not a
  deliberate refusal is logged in full and answered
  `{"code": 500, "message": "internal error"}` instead of echoing its text.
  Deliberate validation errors keep their message and status 400 (D895,
  `9f794d2d`; earlier `a6bdb55c` for `activity_result`, D187). Invalid client
  input such as an unparseable `page_cursor` stays a 400 (`805119fb`).
- **`resolve_object` needs a token** (see above, `390b958f`).
- **No session fallback for a bad token.** An image upload or delete with an
  invalid bearer token is refused instead of falling back to the browser
  session (D880, `d142aa27`). The alpha API keeps answering bad credentials
  with its 400 `incorrect_login`, not 401.
- **Logout** with a token that has no `jti` is a 400 instead of a false
  success (D1182, `0980c2e1`).
- **Comment views gain `filtered`**, mirroring the post API's flag: true when
  a "hide completely" reply filter matched (`dbc59344`).
- **Poll votes**: a vote is accepted only from a user who may reply in the
  poll's community (PERM-2, `f5f10da2`). A single-choice vote for a
  non-integer or unknown choice is a 400 (D415, `f2bf6173`).
- **Post and event URLs** submitted through the API must use `http` or
  `https` (`41bee850`).
- **Distinguish**: a comment's distinguished flag is honoured only for users
  who pass `can_moderate`; otherwise it is ignored (D551, `927c2653`).
- **Moderation**: an author restoring a moderator-removed post gets 403
  (`6a25a43f`); banning an admin requires admin, and user 1 cannot be banned
  (`588ffe6f`).
- **`image_alt_text`** is always applied to the post's image (D856,
  `f5736f54`).
- **Community edit**: the shared `edit_community` now sets every setting the
  web form sets, so an API edit can change them (D641, `de888896`).
- **Feeds**: renaming a subscribed feed is refused (`da09d3a6`); NSFW/NSFL
  feed creation follows site policy (`27002782`).
- **Leaving a community** you are not in is an idempotent success, not a 500
  (D598, `ca56967b`). Repeating a post delete or restore is a no-op
  (`b3d02e37`).
- **Upload size**: bodies over `MAX_CONTENT_LENGTH` get a JSON 413
  (`c2a8c817`).
- **Many "id nobody holds" 500s are now 400 or 404**, for example six view
  helpers (`abb4d286`) and nine query-string integer sites (`ccf0a906`).
- **Permission refusals** are logged at info level without a stack trace or
  Sentry event (D537, `49afb466`); this is server-side only.

### 2.5 Federation and wire-format changes (for other implementers)

**Responses this instance serves**

- **410 Tombstones.** A deleted local post is served as 410 with an
  ActivityPub `Tombstone`; private, local-only and unpublished posts are 403;
  401 when the author blocks the requesting instance. Visibility is checked
  first, so a Tombstone never confirms the existence of something the caller
  may not see. `post_ap`, `post_replies_ap` and the post context share this
  gate (D189, D199, D190, `f22ce3e1`, `7f5119c9`). A remote reply's URL
  redirects to its origin and a deleted local reply is a Tombstone (D191,
  `0051d1a8`).
- **Cache-Control policy.** Every ActivityPub endpoint takes its
  `Cache-Control` from one table: actor profiles 300 s, content objects
  120 s, collections 60 s; 404 and 410 responses are `no-store`
  (D160, D180, D195, `5bd52b3d`; D196, `87e95916`). Feed profiles now send
  `Vary: Accept` (D161, `f1bb26bb`).
- **Outbox paging.** User, community and feed outboxes keep `totalItems`
  (now a real count, `0dc59302`) and their first page inline, and add `first`
  and `last`. `?page=N` serves an `OrderedCollectionPage` of 50 items with
  `partOf`, `next` and `prev`; any value other than a positive integer is a
  400 (D173, `64831764`).
- **Collections.** Feed collections list moderators by `public_url()`, use
  the lowercase id, and the feed outbox is an `OrderedCollection`
  (D183-D185, `4d8766a4`). The moderators collection lists real moderator
  rows only, with no synthesised owner (D174, `16883c7c`). A community's
  featured collection lists only published posts (D179, `48588b68`).
  A followers collection hides followers the user has blocked (D182,
  `411ce71f`). A browser asking for an ActivityPub collection is redirected to
  the HTML page (D177, `ba97c416`).
- **Webfinger is stricter.** A resource for another domain is 404 instead of
  serving the same-named local actor (D145, `a1a7836d`); a resource is parsed
  as `acct:` only when it starts with `acct:` (D146, `333590fa`); malformed
  resources are 400 (`7a068350`); private, banned and deleted feeds and
  communities are no longer advertised (`746d8f7c`, `be337f9e`); a user's
  private-voting `alt_user_name` no longer resolves (D166, `444ad96f`); feed
  names match case-insensitively (D148, `98aa5752`).
- An ActivityPub request for a remote user's profile, `/u/<user@host>`, is a
  400, as it already was for remote communities (D156, `698509cf`). An HTML
  request by a signed-in user resolves the remote handle instead
  (`4df52134`).
- An ActivityPub GET no longer creates an `Instance` row for its caller
  (D193, `f5e31877`). The author instance-block check identifies a signed GET
  by its signing key's host, falling back to the User-Agent (D194,
  `c48eb206`).
- `/quote_boost_auth` vouches only for quotes this instance recorded accepting
  and answers 404 for anything else (R205, `8e83df63`).

**Activities this instance sends**

- **Flag shape for chat reports.** Reporting a conversation with "report
  remote" ticked sends the reported member's instance a `Flag` whose object
  lists the reported account's actor first, then the message ids, as Mastodon
  and Lemmy expect (D761, `4275f499`, `0d0f3353`).
- **Events.** A federated Event omits optional properties it does not have
  instead of sending `null` (D306, `707188a1`). An event post's URL is
  attached as a `Link` on both send paths (D300, `b0b04417`), and the new
  "More info" URL is an extra attachment `{type: Link, href, name: "More
  info"}`, read back on ingest (R223, `0079d346`). A null timezone is
  localised to UTC (D307, `88c2b723`).
- **Announce batches capped at 100.** Our batched Announces carry at most 100
  objects (D54, `c131496c`). The limit is `ANNOUNCE_MAX_OBJECTS`.
- **Accept re-send.** A re-sent Follow for an already-accepted relationship
  gets the Accept again; one for a pending relationship is logged and ignored
  (D66, `f9805584`).
- An Undo of a Follow carries the original join request's id for every peer,
  not only ovo.st (D89, `0c809cc0`).
- **Retry policy.** An outbound activity whose connection fails (refused,
  DNS, timeout) is queued for retry on the same exponential backoff as a 429
  or 5xx (D767, `2d99481e`). Inbox processing never retries in-line (D775).
- **Outbound DNS pinning.** All outbound HTTP through the shared httpx client
  (and the Stripe client) resolves a host once, refuses it if any address
  fails the SSRF guard, and connects only to a checked address. The URL is
  unchanged, so the `Host` header and TLS SNI still carry the hostname, and
  each redirect to a new origin is checked again (R162, `edee89f0`). The
  LibreTranslate client is deliberately left unpinned because its endpoint is
  the admin's own, often on a private network.
- Private and local-only communities no longer federate deletes, bans, edits,
  votes, locks, reports, moves, chosen answers or replies to other instances
  (for example `fc6343c8`, `c56b9623`, `3711a367`, `d03bbd16`, `9e108edb`).

**Activities this instance accepts**

- Followers-only and direct content is refused at ingest rather than stored
  as if public (`afc133d4`). Direct messages are unaffected.
- An inbound Announce of a list refuses nested lists and processes at most
  100 objects (D54, `c131496c`).
- Content fetched outside the inbox (resolve, search, outbox backfill) must
  pass the same author gates as an inbound Create; a refused item is skipped
  and logged (PERM-3, PERM-4, PERM-5: `1db0afcc`, `fe13b0b6`, `dfdd961e`,
  `af1d878f`). Backfill requires the moderators collection to be on the
  community's own host (`5bdc5935`) and walks a paged outbox from `first`
  along `next` until 50 items or 10 pages (`818a53ee`).
- An inbound edit to an existing post is refused when the editor may not
  post in the community, as reply edits already were (D141, `8dabf9d1`).
  Federated reply Updates follow the post rules (D249-D253, `6f9acb1d`).
- A reply Create carrying `repliesEnabled` stores it (D269, `cfbd75ba`).
- A second vote in a single-choice poll replaces the earlier one (D120,
  `55d66706`).
- Boosts of microblog posts by followed accounts are ingested and surface in
  the feed, and an Undo of such a boost is handled (`e79f03ff`, `b1694566`,
  `9bd4486c`).
- Many malformed inputs that used to raise are now refused and logged: an
  Announce with no id, type or actor (`616d9fad`, `c92ff823`), an
  Accept/Reject of an untyped object (`6b487c85`), a `name: null` post
  (treated as untitled, D258, `04905efb`), an empty `contentMap`
  (`54654020`, `6231560b`), a nameless Hashtag (D246, `6f71323f`).

---

## 3. Security fixes

These are the fixes with a security impact, grouped by kind. Twelve carry the
`security:` prefix; the rest were found by the campaign and committed as
`fix:`.

**SSRF and DNS rebinding**
- DNS rebinding past the SSRF guard closed by connection pinning (R162,
  `edee89f0`); the NNTP server's image fetch now goes through it
  (`b678e44e`).
- The SSRF guard allowed every IPv6 literal, including `[::1]` (`6547187d`).
- Anonymous callers could drive outbound fetches through `resolve_object`
  (PERM-1, `390b958f`) and an unauthenticated outbound request in the post
  fragments (`3659f4e6`).

**XSS and unsafe markup**
- A remote community's posting warning was rendered unescaped (`|safe`) on
  every post page (`3055132f`).
- A post's teaser paragraph decoded entities and re-rendered them as live
  markup in the feed (`bfb1f1b5`).
- URL scheme checks: `is_image_url` accepted `javascript:` (`ca8d796e`);
  three more URL predicates (`191ef94e`); a peer's post URL (`d37cb51c`) and
  Event links (`ce2597f9`); API post and event URLs (`41bee850`);
  `File.source_url` (`7b057461`); object ids on every path (`107004a1`).
- SVG uploads that cannot be sanitised are refused (`fbf39569`).

**Open redirects**
- One origin check for every user-influenced redirect target, replacing a
  substring test (`04730629`, `15fbf52a`), with the admin-configurable policy
  (`13fe5d50`).
- Open redirects on the remote-follow form (`d5d02bbe`), the share button
  (`17063990`) and the anti-bot interstitial (`f3de7b63`).

**CSRF on GET**
- Join, leave, join-then-post, feed add/join/leave and all notification bells
  converted to POST with CSRF (see 2.3). A ratchet test now fails on any new
  mutating GET (`d5856dd5`, extended in `11de66ff`).
- A GET cleared a post's flair (`bf186076`); loading an image could make an
  admin sticky a post instance-wide (`9efc85d5`).

**Authorisation gaps**
- The permission call-site audit's five deferred items, resolved: anonymous
  `resolve_object` (PERM-1, `390b958f`), poll voting with no permission check
  (PERM-2, `f5f10da2`), fetched content stored without ban or allowlist gates
  (PERM-3/4, `1db0afcc`, `fe13b0b6`, `dfdd961e`), outbox backfill attributing
  posts to any instance (PERM-5, `5bdc5935`, `af1d878f`). Earlier audit
  findings F5, F7, F10, F12 and F13 were fixed (`8dabf9d1`, `2d5d3937`,
  `51434aad`, `4abd7816`, `b781a6b6`); `choose_answer` / `unchoose_answer`
  gained a check (`25ec35e9`).
- Anyone could silence anyone's post, and a post could be moved into a
  private community (`f179a759`).
- A moderator of any community could undo another community's moderation
  (`2cc1672d`), or repoint or delete another community's RSS importer
  (`70b688ec`).
- An unban anyone could trigger (`1ad9eca9`); a community ban that did not
  stop joining (`1334afe8`); a banned account could file reports
  (`7faf48e2`).
- Any account could put any private conversation in front of the admins
  (`c93157ba`).
- The OAuth signup path bypassed every user-name rule (`a78c226f`).
- `invite_with_chat` and `invite_with_email` now check `can_invite`
  themselves (D632, `3d695124`).
- Five form `validate()` overrides discarded `super()`'s verdict
  (`009ae56c`).
- A username with regex metacharacters could claim feed URLs in another
  user's namespace (`d0561e82`).

**File and storage safety**
- A peer could delete any file on this server by naming it in a post's image
  URL (`15a0a7d6`), or any object in the S3 bucket (`74c05ae0`).
- Imported ban lists are no longer written into the served media directory
  (D930, `95a2357b`).

**Private community and private instance leaks**
- A private community's page refused to non-members (`3801bcdc`); private
  communities no longer offered as a post destination (`39a9e22a`); the
  private-community filter applied for every feed viewer (`54af1753`).
- Leaks through the author's RSS feed (`4bb2520a`), the cross-post form
  (`bfa0588d`), the front page (`8f783fad`), a profile tab (`d89b0ee1`), the
  embed pages (`bf186076`), the ical, tag and topic routes (`3659f4e6`,
  `e39d50f7`, `b005f091`) and several more (`17063990`).
- Private feeds hidden from every route that takes their id (`59fd5238`,
  `fa9b9810`).
- ActivityPub post and reply endpoints enumerated replies of private,
  local-only, unpublished and deleted posts (`f22ce3e1`).
- The private-instance gate extended as listed in 2.3.

**Information disclosure**
- Every uploaded file's URL was enumerable by id (`d9c1d9af`).
- Internal exception text is no longer returned by the API (`9f794d2d`,
  `a6bdb55c`) or leaked from a peer's refusal (`599c6b7f`).
- Passkey endpoints no longer reveal whether a username exists or has
  passkeys (`8c859131`, `7ed637ca`).
- The feed cache key is scoped to the user, not a client-supplied result id
  (`21f36da2`).

**Token and session revocation**
- A password reset by any route revokes API tokens (`8888ab77`).
- An RSS token kept working after the account was banned or deleted
  (`9bba6b4d`).
- An invalid bearer token no longer falls back to the session (`d142aa27`).

**Client IP and rate limits**
- The client IP was read from client-controlled headers (`9c215e2a`); the
  rate-limit exemption was a substring test (`d7ca6b51`).
- The registration honeypot was a bypass (`dae8ff11`, `487f3b62`).

**Signatures and replay**
- The admin replay refuses an activity that originally failed signature
  verification (D45, `d105d2e8`); a value-truncating Signature header parser
  was fixed (`155a5daa`) and the two parsers merged (D768, `041f8d94`).

---

## 4. Other fixes by area

The 699 `fix` commits are summarised here by theme. Counts are by the
application module each fix changed most (fix, security, feat and perf
commits together): `app/activitypub` 233, `app/shared` (shared actions and
background tasks) 150, `app/utils.py` 53, `app/models.py` 44, `app/api` 36,
`app/community` 31, `app/admin` 23, `app/feed` 20, `app/user` 19, `app/post`
17, `app/main` 14, `app/auth` 12, templates 10, smaller modules the rest.
About 95 touched only tests or tooling.

**ActivityPub inbox.** The largest group. Typical defects were activities
that raised on unexpected shapes (single embedded `attributedTo`, D38,
`18a37df8`; host differing only in case or port, D21/D24, `0b65e440`,
`8b7db46f`), counters left wrong (a Reject or unfollow lowering follow counts
for a follow that was never accepted, D75/D557, `98821e4a`, `4d182ae6`),
rejecting a follow that accepted it (`d9c1d9af`), and federated posts that
were never created at all (any post mentioning a local user, `ce708b20`; every
post when there was no `Site` row, `bfb4106d`). A flagged domain silently
discarded posts; moderators are now notified instead (`3b311b0a`), local
moderators only (D288). Logging was corrected so that each outcome is
recorded under its real type (D63, D68, `2faabef8`, `e3769943`).

**Outbound federation.** Sends that crashed on missing data now federate (a
post whose Poll or Event row is missing, D490, `87caa8bd`; a send with no
body, D768, `3c439b78`); a delete's follower fan-out skips dormant instances
(D335, `b7acd013`); sticky no longer federates a change it refused to make
(`5da7e47e`); a delete that never left the instance (`5a655ad5`); the
private-community send gates listed in 2.5.

**Content ingest and display.** A post could be missing from every page of a
sort (`097b8a65`); an unpublished post was public (`0bbb4843`); an Update
whose URL cannot be stored keeps the existing one (R213, `18eb10c5`); a peer
Event was lost unless it carried all fourteen optional fields (`8f90e2ad`);
`rewrite_href` now rewrites a URL that matches a local post (`78dc90ea`);
attachment pre-checks scan every attachment, not only the first (R202,
`859fcb26`); a stored post URL that 500'd every listing (`0a5780bf`).

**Moderation and reports.** One moderation predicate (`2ebd72ce`); reports
name the right source instance (D440, D550, D553, `5c79bae3`, `45e889a9`);
the minor-abuse escalation actually fires (`3a7a4165`); a retransmitted
instance Block is a no-op (D205, D94, `a5754d34`); bans decrement cross-post
reply counts (D206, `f85a528c`); an admin or staff member with several roles
can no longer be blocked (D558, `d56cc967`); local communities gain an NSFL
control (R203, `67e7439d`).

**Communities.** Leaving as a non-member is idempotent (`ca56967b`); the web
edit form goes through the shared `edit_community` (D641, `da0138f2`);
community refresh skips malformed featured or moderator entries (D219,
`e32fec1b`) and creates unseen actors only on the collection owner's host
(D226, `9bca2d32`); a ban window measured against the wrong clock
(`fbab3e20`).

**Feeds.** Every private feed was a 500 for its owner (`5539540e`); taken URLs
on create and copy are refused with a message (D681, `9d2c1412`, `e7c0b77e`);
copying redirects to the owner's list (D721, `ae445b60`); a failed remote feed
join leaves no membership behind (D682, `8f4be523`); a feed with a null owner
publishes an empty moderators collection (D170, `bea245d2`).

**Users and authentication.** Malformed password hashes are a failed login,
not a 500 (`a0e798cf`); an ipinfo.io outage no longer fails logins
(`d176db1a`); messaging someone again reuses the conversation (D748,
`00149969`); user 1 cannot delete itself (`7faf48e2`); NULL `verified` is
treated as unverified (`054b2a0c`).

**API.** Besides the contract changes in 2.4: three reply verbs refused
unauthorised callers (`0b4ecadd`); API image alt text (`f5736f54`); many
missing-id 500s replaced by 4xx.

**Admin.** The permissions page could delete a permission for good
(`b2b76bae`); a Save that closed the instance (`b781a6b6`); the admin reports
search box did nothing (`ecd75126`); role names spelled once as constants and
matched by name (D965, D481, `f659c10d`, `32b49df5`); a domain entered on the
federation page is normalised once (D929, `7758c223`); blocking an unknown
domain or this instance's own domain is refused (D581, `62cc80c3`).

**Background tasks.** Tasks use their own sessions (D312, `0272180d`); a
community with no instance is treated as undeliverable (D331, `bea4ef2b`);
`refresh_instance_chooser` uses a SAVEPOINT per domain (D364, `9eb2a4d3`);
stats tasks commit per row with consistent lock order (D342/D343,
`bd3d62c1`); a retried new-post fan-out skips users already notified (D278,
`5d7c94be`); S3 clients are closed on failure (D363, `b5bb77e0`).

**Uploads and media.** Besides the size and format changes in 2.3: uploads no
longer reset Pillow's process-wide pixel ceiling (D467, `5ed3cbf2`); an SVG
banner was a 500 (`17c88fe4`); no post image was ever purged from the CDN
(`199044b4`); thumbnails on a URL change obey the remote-image caching setting
(D289, `db6f2cb8`); the PeerTube embed rewrite is anchored (R251,
`a15a281f`).

**Search.** Logged-out search never returns NSFW (`baea0c81`); a URL search
searched nothing (`8f783fad`); a searched reply reordered its thread
(`898790d4`).

**NNTP.** The NNTP server's image fetch goes through the SSRF guard and the
pinned client (`b678e44e`); NNTP login goes through `User.check_password`, so it strips
passwords like every other path (`9d77ebf3`).

**Plugins.** A `before_post_create` plugin's returned title and content are
now used, and a handler returning `None` no longer nulls the data (D811,
`3c2b43e9`); `load_plugins` hands out a copy of the registry (D812,
`b0757c60`); `FLASK_DEBUG=true` stopped every plugin from loading
(`2598a37c`); `/webhook` requires `WEBHOOK_SECRET` (see 2.2, `298301cb`).

### Late fixes

Residues of earlier fixes, found after the register was closed:

- A defederation-list download that fails (unreachable, not 200, not a
  usable list) is logged and leaves that subscription's bans untouched;
  only a genuinely empty list clears them (D378 residue, `0b39bba7`).
- A remote feed join that fails after its Follow was sent also sends the
  Undo{Follow}, so the peer does not keep us subscribed (D682 residue,
  `b75aac27`).
- Following a user you already follow, or have asked to follow, is a no-op:
  no second row, count or Follow (R265 residue, `ad3fab15`).
- On a private instance a member with no RSS token gets one wherever a
  page renders its RSS link, not only on the front page (R219 residue,
  `4a045d98`).
- A moderator or feed owner listed after the 50th collection entry keeps
  the role on refresh; already correct, now pinned by tests (D225 residue,
  `109681e2`).
- Passkey login options give an unknown name, or an account with no
  passkey, one stable decoy credential, so they cannot be told apart from
  an account with one (D888 residue, `34c6da47`).
- The community page and `do_subscribe` check bans through
  `user_banned_from_community` alone; the bulk-import result reports every
  ban as `user_banned` (D995 residue, `0c68500a`).
- Inbound activity log labels: a Page arriving through the chat fallback is
  logged as Create (D130 residue, `e9bb345b`); a Mastodon-style Block is
  logged as User Block, a new `APLOG_USERBLOCK` (D83 residue, `9e404a38`).
- Closed, not a defect: R223 residue (an Update omitting the More info Link
  clears it), because an Update replaces the object.

---

## 5. New features

The twelve `feat` commits:

| Commit | Feature |
|---|---|
| `caa61836`, `4736350e`, `54340319` | Helpers for reading Announce activities, boost cache entries, and boost recording and removal |
| `e79f03ff` | Ingest boosts of microblog posts from followed accounts |
| `b1694566` | Handle Undo of microblog boosts (boost counts could previously only rise) |
| `9bd4486c` | Posts boosted by followed accounts appear in the feed |
| `9553c643` | An ActivityPub visibility classifier |
| `afc133d4` | Refuse followers-only and direct content at ingest |
| `4df52134` | Resolve unknown remote handles at `/u/<user@host>` for signed-in HTML requests |
| `7ed73727` | Admin tool to follow accounts from a Mastodon server's directory |
| `13fe5d50` | Configurable redirect policy |
| `9630e07c` | A read-only report of actor rows whose profile host disagrees with their domain |

Features finished or added through `fix` commits under owner rulings:

- Event "More info" link: stored, rendered, federated and ingested (R223,
  `0079d346`).
- Reply keyword filtering, with the comment API `filtered` flag (R168,
  `dbc59344`).
- Outbox paging (D173, `64831764`), with paged backfill (`818a53ee`).
- Chat reports federated as a `Flag` (D761, `4275f499`, `0d0f3353`).
- Quote authorisation records (R205, `8e83df63`).
- Private-instance RSS by member token (R219, `2862e8bf`, `9c552a79`).
- "Request declined" state for refused follows (R265, `4e99e002`).
- NSFL control for local communities (R203, `67e7439d`).

---

## 6. Internal and developer changes

**Import cycles.** The circular import between `community.routes` and
`activitypub.routes` was broken (`2e72e644`), then six further cycles
(`434447bd`, `cecab663`, `8f2c195f`, `07fe217f`, `bac0dbaf`, `ddd484c9`) and
the cycles under `app.shared` and `config` (`2c67f718`, `23dfdc71`). The test
conftest no longer pre-imports `app.activitypub` to hide them (`ffce895e`).
`tests/test_import_order.py` imports every module under `app/` first in a
fresh interpreter, so a new cycle fails the suite.

**Inline-import ratchet.** Of 216 function-level imports in `app/`, 131 were
hoisted to module level (the `refactor: hoist inline imports` series) and 85
were kept with a tag explaining why: 69 `# cycle:` and 16 `# lazy:`.
`tests/test_no_inline_imports.py` fails on an untagged function-level import
and on any increase in the kept count (`278a0065`).

**Mutating-GET ratchet.** `tests/test_mutating_get_routes.py` fails when a GET
route writes, directly or through a shared helper (`d5856dd5`, `11de66ff`).

**One moderation predicate.** `can_moderate` in `app/utils.py` states the
moderation rule once; `can_mod_post` delegates to it (`2ebd72ce`). Similarly,
one helper decides community bans (`e5fc1027`), one names the user-1 ban
exemption (`e991e2de`), one parses the Signature header (`041f8d94`), and one
decides redirect safety (`04730629`).

**Other refactors.** 841 call sites moved off SQLAlchemy's legacy
`Query.get()` (`ca932707`); request wiring moved into the app factory
(`77d25918`); dead code and debugger stubs removed (for example `84bca160`,
`904f98d9`).

**Test harness.**
- `run_tests.sh` runs the suite against disposable database containers
  (`a195c2be`, `4926aef8`); `compose.test.yaml` builds the Dockerfile's
  `test` stage.
- `tests/factories.py` provides model factories (`a195c2be` onward).
- The suite runs on four pytest-xdist workers, each with its own database and
  Redis databases (`c82b54f1`), with `xdist_group` load groups honoured even
  for plain `-n` runs (`389b4ece`). Tables are cleared by deleting rows rather
  than truncating (`4bfe4858`).
- Per-module coverage floors in `coverage_floors.ini`, checked by
  `tests/test_coverage_floors.py`, only move up.
- The suite has 17,142 passing tests.

**The findings register.**
`docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` records
every defect found, with reproduction evidence and, for each fixed row,
"FIXED at `<sha>`". Its closing section lists the prose findings, closures
and lifted deferrals. The companion
`docs/superpowers/specs/2026-08-25-permission-callsite-audit.md` records the
inverse permission audit; its Status section records the resolution of
PERM-1 to PERM-5.

---

## 7. Known remaining items

- **D354 stays deliberate.** Per-row commits inside loops are kept as ruled;
  this is the only item the register still lists as deliberate.
- **Closed as not defects:** D203 (restore's cross-post recompute from NULL
  is correct) and D599 (nothing memoizes community notification
  subscriptions).
- **Won't-fix, recorded:** D157 (confirmed unreachable over ActivityPub since
  D156, pinned by `d72d8ec0`), D450 (dead guard removed, `5484a9a1`).
- **Converted form buttons are not browser-verified.** The Join, Leave,
  notification-bell and feed buttons that changed from links to POST forms
  are covered by tests but have not been checked by hand in a browser.
  Reviewers should exercise them, with and without JavaScript, before
  release.

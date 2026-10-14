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

After the register closed (2026-10-01 to 2026-10-13), work moved to feature
and hardening tracks, each with a design spec under `docs/superpowers/specs/`:

- ActivityPub audience: followers-only posts and replies, stored, enforced on
  every surface, then accepted and created locally (`2026-10-01-activitypub-interop-design.md`).
- Content warnings, multi-image galleries and sensitive-image blur.
- Discovery of PeerTube channels and Castopod podcasts, with a proactive sync
  run by the instance actor (interop D24).
- A Live view that streams new posts into microblogs and home pages.
- ActivityPub relays.
- Microblog server views: `/c/microblogs@<host>`, listed among communities and
  joinable.
- The ActivityPub security track: LD-signature fixes, RFC 9421 signatures,
  secure mode, verified Announces, capped outbound fetches
  (`2026-10-10-ap-security-track-design.md`).
- Interop tracks C1 to C5: actor and collection completeness, quote posts,
  multi-language content, media-type objects, count-only collections.
- Fuzzing every input surface with atheris, with each finding fixed
  (`2026-10-13-fuzz-all-input-surfaces-design.md`).

The range contains 3,143 commits:

| Type | Commits |
|---|---|
| `fix` | 1,103 |
| `test` | 1,074 |
| `docs` | 699 |
| `feat` | 146 |
| `refactor` | 47 |
| `chore` | 39 |
| `security` | 12 |
| `perf` | 6 |
| other (`revert`, `build`, `deploy`, untyped) | 17 |

The test suite collects 22,420 tests.

---

## 2. Upgrade notes

### 2.1 Database migrations

Twenty-five Alembic migrations were added. They form a single chain on top of
`7b8bf43fa079`, which is already present in v1.7.17, so `flask db upgrade`
applies them in the order below. Several of them rewrite data; none delete
rows.

The NOT NULL migrations among the first four use a lock-friendly recipe: each adds a
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
| 9 | `a7e3f1c2b9d4` visibility | Adds `visibility` (NOT NULL, default `public`) to `post` and `post_reply`, each with an index. The column is instant on PostgreSQL 11+, but the indexes are built with a plain `CREATE INDEX`, which blocks writes to each table while it builds. (`0768f2c0`) | No |
| 10 | `b5d2c8e1f7a3` content_warning | Adds `content_warning` to `post` and `post_reply`, and `post.gallery_count` (default 0). (`f47c4405`, `a49b7794`) | No |
| 11 | `c6e1d9a4b7f2` discovery_entry_and_post_extensions | Creates `discovery_entry` and adds the JSON `post.extensions`. (`9397ffad`) | No |
| 12 | `d7f3a9c2e4b1` ap_activity_ids_255 | Widens `ap_create_id` and `ap_announce_id` on `post` and `post_reply` from 100 to 255 characters. (`1d2c7bdc`) | No |
| 13 | `e8a4c1f9d2b6` discovery_sync | Creates `discovery_sync`, the communities the instance actor keeps synced. (`28fe5c0a`) | No |
| 14 | `f1b7d3a8c5e2` discovery_exclusion | Creates `discovery_exclusion`, actor URLs the sync must never re-add. (`15c826f2`) | No |
| 15 | `a3c9e5f1b7d4` rerank_backfilled_posts | Recomputes `ranking` (and shifts `ranking_scaled` by the same amount) for every post whose `posted_at` is more than an hour before `created_at`, so that backfilled posts sort by when they were published. (`37cc531e`) | Yes, those rows. Not reversible |
| 16 | `b7e1c2d9f4a6` relays | Creates `relay` and adds a nullable `post.relay_id`. The foreign key is added `NOT VALID` and the index is built `CONCURRENTLY`, so `post` is never locked for a scan. (`95a97f1f`, `9f06ce09`) | No |
| 17 | `c4a8e2f61d3b` instance_signature_format | Adds `instance.signature_format` (default `cavage`), which records whether a peer takes RFC 9421 signatures. (`0d7a25eb`) | No |
| 18 | `e5c1d8a2b7f9` strip_non_open_cross_posts | Removes the ids of followers-only posts from every `post.cross_posts` array, clears a followers-only post's own list, and recounts. No row is deleted. (`dc74d42a`) | Yes, matching rows. Not reversible |
| 19 | `d9f3b7a15c2e` site_require_signed_fetch | Adds `site.require_signed_fetch` (default false), the secure-mode switch. (`bf44e5ee`) | No |
| 20 | `e2a7c4f9d1b3` report_ap_id | Adds `report.ap_id`, so an Undo of a Flag can find its report. (`eb53e6da`) | No |
| 21 | `f4b8d2a6c1e7` user_outbox_indexes | Builds `(user_id, posted_at DESC NULLS LAST)` partial indexes on `post` and `post_reply` `CONCURRENTLY`. A rerun after an interruption rebuilds an invalid leftover. Needs Alembic 1.12 or later, now pinned. (`35275518`, `057e6dd2`, `86d5ce95`) | No |
| 22 | `a7c3e9f1b5d2` quote_ap_id | Adds `quote_ap_id` to `post` and `post_reply`. (`13f3455c`) | No |
| 23 | `b8e2d4f6a1c3` language_variants | Adds the JSONB `language_variants` to `post` and `post_reply`. (`a4c1460f`) | No |
| 24 | `c9f3a5b7d2e4` vote_federated_like | Adds the nullable `federated_like` to `post_vote` and `post_reply_vote`. (`f6cec283`, `df3e8cce`) | No |
| 25 | `d4a8e2c6f1b9` server_view_member | Creates `server_view_member` (user, instance), the joins of microblog server views. (`c91d5054`) | No |

Downgrades remove defaults, constraints, columns and the new tables, but do
not restore the NULLs, invented counters, rankings or cross-post links that
the upgrades replaced; there is no record of which rows held them.

### 2.2 Configuration

New or changed settings in `env.sample`, `env.docker.sample` and `config.py`:

| Setting | Default | Effect |
|---|---|---|
| `TRUSTED_CLIENT_IP_HEADER` | empty | Where the client IP is read from for rate limiting, IP bans, the honeypot ban, geolocation and the `user.ip_address` audit column. Empty means `request.remote_addr`, which `ProxyFix(x_for=1)` already takes from the last `X-Forwarded-For` entry your proxy appended. **Behind a single Caddy or nginx proxy, leave it empty.** Behind Cloudflare or a similar CDN, set it to the CDN's header (for example `CF-Connecting-IP`), and only if clients cannot reach PieFed directly. If the header holds a list, the last entry is used. Previously PieFed read `CF-Connecting-IP` and then the *first* `X-Forwarded-For` entry, which the client controls. (`9c215e2a`) |
| `WEBHOOK_SECRET` | empty | Shared secret for the plugin `/webhook` endpoint. Empty disables the endpoint (it answers 404). When set, a request must carry it in an `X-Webhook-Secret` header (compared in constant time) or it is refused with 403. **If you use a webhook plugin, set this and configure the sender, or the plugin stops receiving calls.** `docs/PLUGINS.md` is updated to match. (R223, `298301cb`) |
| `MAX_CONTENT_LENGTH` | `104857600` (100 MB) | Largest request body accepted, in bytes. Larger requests are refused with 413 before the body is read (a rendered page on the web, JSON under `/api/`). Image uploads are separately capped at 10 MB, so raise this only if you allow large video uploads. (`c2a8c817`) |
| `SKIP_RATE_LIMIT_IPS` | `127.0.0.1` | Parsing changed. It is now always a list: a comma-separated string from the environment is split and trimmed. Previously, once set from the environment it was a plain string and the membership test was a *substring* test, so `192.168.1.10` also exempted `92.168.1.1` and `2.168.1.1` from the login rate limits (D1396, `d7ca6b51`). Existing comma-separated values keep working. |
| `redirect_policy` (admin setting, not `.env`) | `same_origin` | Stored in the `Setting` table and chosen on the admin Misc page. Controls which hosts `?next=` and `Referer` redirects may go to: `same_origin` (this server only), `trusted_servers`, `federated_servers` (known, not banned, not gone) or `all_referrers` (labelled dangerous). The default keeps the new strict behaviour. URL parsing rules do not change with the policy. (`13fe5d50`) |
| `SIG_REQUIRED_HEADERS_ENFORCE` | `0` | When `1`, an inbound HTTP signature that does not cover `(request-target)`, `host`, `date` and (on a POST) `digest` is refused. At `0` the gap is only logged as a warning. Review the warnings before turning it on. It is read in `config.py` but not listed in `env.sample`. (`a823ca72`) |
| `CELERY_QUEUES`, `CELERY_CONCURRENCY`, `CELERY_PREFETCH_MULTIPLIER`, `CELERY_MAX_TASKS_PER_CHILD` | `celery,send`, `4`, `1`, `1000` | Read by `entrypoint_celery.sh`. **The default worker no longer consumes the `background` queue.** See 2.2.1. (`261ee07b`, `e001cb39`) |
| `LANGUAGES` (`config.py`) | | The interface now also offers Basque, Finnish, Hindi, Italian, Korean, Polish and Russian (`122ee9f7`). |
| `require_signed_fetch` (admin Federation page) | off | Secure mode. ActivityPub GETs must be signed by an actor this instance already knows. Unsigned fetches of user, community and feed actors get a minimal document (inbox and key only); everything else is refused. Signed answers are kept out of shared caches. (`bf44e5ee`, `5f122f92`, `4adc5993`) |
| `cross_origin_delete_delay` (admin Federation page) | 90 seconds | How long a moderator's Announced Delete of a post from another server waits before it is applied, unless the origin confirms the deletion first (`4bab1542`). |
| `relay_retention_days` (admin Relays page) | 7 | Relayed posts that nobody here engaged with are purged after this many days (`9f518b3b`). |
| `discovery_sync_per_host` (admin Discovery tab) | 0 (off) | How many of each host's top PeerTube channels and Castopod podcasts the instance actor follows and polls. Proactive sync does nothing until it is set (`b09cc6b9`, `1c4932f9`). |
| `discovery_external_search` (admin Discovery tab) | on | Lets search include PeerTube videos from the wider network through SepiaSearch. Each viewer must still tick the option (`8b3cdbb1`). |

`config.py` no longer imports `app.constants`; it reads `VERSION` from
`app/constants.py` by path so that `import config` does not cycle through
`app/__init__.py` (`23dfdc71`). No action is needed.

Packaging: `pytest` was removed from `requirements.txt`. Test-only
dependencies (pytest, pytest-cov, pytest-timeout, pytest-xdist, respx, moto,
fakeredis, atheris) live in the new `requirements-test.txt`, installed only by
a new `test` stage in the `Dockerfile`, which sits above `runtime` so the
default build is still the production image (`15fbf52a`). atheris has no
aarch64 wheel, which is why it must stay out of the production install.

#### 2.2.1 Deployment

- **A second Celery worker is required.** `entrypoint_celery.sh` now runs the
  queues named in `CELERY_QUEUES`, by default `celery,send` (the inbox and
  outbound delivery). Backfills, discovery sync and maintenance run on the
  `background` queue, so no long task can hold the slots the inbox needs
  (`261ee07b`). `compose.yaml` adds a `celery_background` service with
  `CELERY_QUEUES=background` and `CELERY_CONCURRENCY=2`. **If you run your own
  worker containers or units, add one for `background`, or that queue is
  never processed.** The bare-metal `CELERYD_OPTS` in `INSTALL.md` already
  splits the queues this way. Each slot now prefetches one task, and a child
  process is replaced after 1,000 tasks (`e001cb39`).
- **Proxy route for the Live view.** `/live/stream` must be proxied to the
  notification server (`piefednotifs:8000`) without buffering, like
  `/notifications/stream`. The `Caddyfile` does this, and `INSTALL.md` has the
  nginx `location = /live/stream` block (`f2339925`).
- **Caddy holds requests during a restart.** `reverse_proxy web:5000` now
  retries the dial for up to 30 s. A restart used to answer inbox deliveries
  with 502, and senders that do not retry lost them (`dad78b15`).
- **`daily.sh`** also runs `flask refresh_discovery` and
  `flask sync_discovery` (`d0d3985c`, `a95226c3`).
- **Images and versions.** Python 3.14, Redis 8.10 (from 6.2) and PostgreSQL
  pinned to 17.11 (`3953e392`). Python dependencies were updated to their
  latest compatible releases (`ec2dc863`), among them celery 5.6, pyld 3,
  webauthn 3, stripe 16 and redis-py 6. `alembic>=1.12` is pinned
  (`86d5ce95`).
- **Build context.** `.env` files, every `pgdata` copy and `deploy/` are kept
  out of the Docker build context, so the secrets in `.env` no longer end up
  in the image (`9fc24049`, `7cbc4c7f`). Rebuild any image built from a
  checkout that held a `.env`.
- New CLI commands: `flask refresh_discovery`, `flask sync_discovery`,
  `flask discovery-seed-fixtures` (local validation data, no network) and the
  `flask relays` group, including `flask relays refresh`, which follows every
  accepted relay again after a restart (`ad4a0542`, `0f3f5c97`).

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
`726c5a7d`). The same rule now holds for every resolver and inbox fetch:
`get_request` retries only when a housekeeping task asks it to
(`e2a9f1fd`, `970cf43b`, `ce63a354`).

**Followers-only content.** Posts and replies now carry an ActivityPub
audience: `public`, or followers-only. Federated content records the audience
it arrived with (`c8b4ed53`), and one predicate, `can_view`, decides access in
Python, the ORM and SQL (`25c275ed`). A followers-only item is shown only to
its author's accepted followers, to the author it replies to, and to anyone it
mentions (`e18aea3e`). Everyone else sees this:

- 404 on single-object views (`2dba1f16`);
- nothing in listings, search, RSS and the sitemap (`27b55f43`), or on
  profiles, bookmarks and the user API (`c4acf991`);
- a placeholder in a thread that keeps the reply's public children (`416aad03`);
- notifications that name "a followers-only post" (`5cc642a9`).

Moderators can still remove, restore, lock, sticky and purge such content
without seeing it, and their acknowledgements and the modlog do not reveal it
(`5b3b70fb`, `29617f0b`, `a2413059`). Once every surface enforced this,
followers-only content from peers was accepted instead of refused
(`d326bcf7`). Local users can now write followers-only posts in the local
microblogs community, and replies under them inherit the audience
(`c33554fe`, `1448b8f6`). Such a post cannot be moved, cross-posted,
quote-authorised or prefilled into a form (`538be041`, `d626433d`).

**Content warnings and galleries.** A peer's content warning is kept, and the
post or reply is shown collapsed under it everywhere it appears: teasers, the
post page, reply forms, polls, link previews, RSS and JSON-LD (`f47c4405` and
follow-ups). Every image of a multi-image post is kept in a gallery
(`300fc147`). Sensitive images, the gallery included, are blurred until
clicked, as are images under a spoiler flair (`a3fa2174`, `f0a6cd14`). Gallery
images are hashed against the blocked-image list, and deleting a post deletes
its gallery's files (`4a8635d9`, `6a4d728c`).

**Discovery of video channels and podcasts.** The daily `refresh_discovery`
reads opt-in directories into `discovery_entry`: SepiaSearch for PeerTube
channels, the Mastodon and Pixelfed directories, and index.castopod.org's
public export for Castopod podcasts (`cd7be8f8`, `dd4aaddc`, `9ad42f45`,
`a7053b6b`). Community and people search fall back to these entries
(`eb6a58e7`, `11bbab35`). An admin can turn on proactive sync on the new
Discovery tab: the instance actor follows each host's top channels and
podcasts, polls them and unfollows the rest (`b09cc6b9`, `61a79295`,
`1c4932f9`). A synced community that an admin deletes is never re-added
(`15c826f2`).

**Castopod podcasts.** A Castopod Podcast actor gets a community beside its
user, and its episodes land there instead of in microblogs (`d3e8cd84`,
`dc701d9b`). An episode gets its audio player and cover (`b96bb052`). Its
byline names the hosts and guests from the podcast's RSS feed, and links only
those whose account links back to the podcast (`da7f284f`, `eac8936e`).

**Media tab and video search.** A new Media tab on the home page shows
PeerTube and Castopod posts (`51caf2b5`, `e0dd6b60`). Post search can be
narrowed to videos and podcasts (`2d2ebff0`). A viewer can tick an option to
include PeerTube videos from the wider network (`8b3cdbb1`). Backfilled posts
are ranked by when they were published (`fe6bdf71`).

**Live view.** A Live view now exists on these pages:

- the `microblogs` community, local or on another PieFed instance;
- microblog server views;
- the home page's Subscribed, Local, Popular, Media and All tabs.

New posts stream in over `/live/stream` and are held while the reader is
scrolled down (`8ce0dcc3`, `f72d1470`, `e3401773`, `62b1a772`, `1ec01d8f`).
Live is off under a flair or tag filter. It falls back to polling when the
stream closes (`685120ab`).

**Microblog server views.** `/c/microblogs@<host>` shows the microblogs
community filtered to one server (`9f4b0918`). `/c/microblogs` lists its
busiest servers (`cc15ccf0`). Server views appear in `/communities` and in
search (`4ecd3062`), and joining one brings that server's public microblogs
into Subscribed (`c91d5054`).

**Relays.** Admins can subscribe the instance actor to ActivityPub relays of
both the LitePub and Mastodon styles, on the federation admin page or with
`flask relays` (`199cfb5d`, `ad4a0542`). Relayed posts are marked. They are
kept only when they are top-level and wanted, and a relayed reply is kept only
in an open thread (`ea0470bb`, `87639c2f`). Relayed content is never
re-broadcast (`e1cbee06`). A relayed post that nobody here engaged with
expires after the retention period (`9f518b3b`).

**Quote posts.** An inbound post or comment that quotes another records the
quoted id, and its `RE:` fallback link is stripped (`13f3455c`, `3a8b0569`).
The quoted object is shown as a card only to a viewer allowed to see it, and
as a plain link otherwise (`1ac08c0b`, `0d7fc9b0`). A card respects the quoted
object's content warning and the quoting item's NSFW blur (`bb7d94af`,
`c6a209be`).

**Multi-language content.** A peer's `contentMap`, `nameMap` and `summaryMap` are kept
as language variants (`9d6a7d02`). Each viewer sees the variant in their
language, with an "Also in" line to switch (`d197afb9`, `8e66afc2`). Blocked
phrases and keyword filters check every variant (`07238370`, `34b9be0c`).
The translate buttons translate the variant shown (`f6d1860c`).

**Media objects.** A Create of a bare `Audio`, `Image`, `Document` or `Video`
object becomes a post (`7b967d8f`, `36ad000a`), and an Audio post plays on
its page even when its URL has no audio extension (`97cc40e3`).

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
- **Followers-only content** is "not found" to a caller who may not see it,
  including in `resolve_object` (`9f5a6c05`, `691db0bc`). In a thread, a
  hidden reply is a stub with neutral creator, community, post and count
  objects, never nulls (`95e2cc4e`). The user API obeys the same predicate
  (`c4acf991`).
- **New `extensions` fields** on posts and replies: `content_warning`
  (`1fc05cae`), the gallery's images (`300fc147`) and, for podcast episodes,
  `podcast.credits`, listing only verified credits (`6b5f393a`).
- **Quotes.** Posts and comments show the id they quote, except when deleted
  (`b245b9d2`, `0d4fc9cd`).
- **Language variants.** Posts and comments return their language variants
  (`6ced9c36`).

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
- **JSON-LD content type.** A peer asking for `application/ld+json` gets
  documents served under that type (`43bbf562`).
- **Followers-only objects** are served only to a signed fetch by an entitled
  requester, or by anyone they were pushed to. Answers that depend on the
  signer send `Vary: Signature` (`f86da461`, `2902a372`, `d91f2f19`).
- **Secure mode** (`require_signed_fetch`, see 2.2) refuses unsigned
  ActivityPub GETs. Actor documents fall back to a minimal form (`bf44e5ee`,
  `4adc5993`).
- **New and completed collections.**
  - A Person names count-only `followers`, `following` and `liked`
    collections (`b36b9b12`, `1eb479d6`).
  - A Group names a count-only `following` collection (`763ccbb8`).
  - A user's outbox lists their public posts and comments, and is refused to
    an instance the user blocked (`35275518`, `201f37dd`).
  - Local posts embed count-only `likes`, `dislikes` and `shares`
    collections, and local comments `likes` and `dislikes`. These are also
    served at their ids (`fdf01a9e`, `d6dbfd1a`,
    `b287464a`).
  - `liked` counts only votes whose last federated activity was a Like
    (`f6cec283`).
- **Private communities.** A private or local-only community's outbox, and
  its featured, moderators and followers collections, are refused
  (`fba2a79c`, `5539201f`).
- **Tombstones.** A deleted local user answers 410 with a Tombstone
  (`39fbe946`). A deleted post's Tombstone carries the type it was federated
  as (`f6e07705`).
- **New endpoints.** `/private_message/<id>` is served to the parties to a
  DM and is 404 to everyone else (`5cef7bc1`). `/actor/inbox` and
  `/actor/outbox`, which the instance actor advertises, now exist
  (`9220eadd`, `ff3c6d5a`). `/activities` serves a logged activity only to
  someone its addressing entitles (`ec22836d`).
- **Discovery links.** Post, profile, community and feed pages advertise
  their ActivityPub id as `rel="alternate"`, in a `<link>` tag and a `Link`
  header, but only when the id is a clean ASCII URL (`4fb74316`, `a231caef`, `b59abbe9`).
- The instance actor has a name, so Castopod can store it and accept its
  Follow (`805b4781`).

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
- **RFC 9421 double-knock.** Outbound deliveries and signed GETs are signed
  in the format the peer is known to accept, cavage by default. A 401 gets
  exactly one retry in the other format (RFC 9421 HTTP message signatures),
  and the format that succeeded is recorded in `instance.signature_format`
  (`0d7a25eb`, `723c3bda`).
- **Followers-only posts** written here federate to the author's followers
  (and anyone mentioned) only, never through the community. The same goes for
  their replies and Deletes (`2992fb92`, `1448b8f6`, `3350b75e`). Activities
  about a followers-only object (votes, moderation, poll votes) are not
  relayed to a community's followers (`15f8c664`, `a8b7443d`, `980d46ac`).
- **Relay subscriptions.** The instance actor sends Follows to relays. A
  Mastodon-style relay Follow is addressed to nobody, as Activity-Relay
  requires (`199cfb5d`, `a20c66de`).
- **Discovery sync.** The instance actor follows and unfollows synced PeerTube
  channels and Castopod podcasts. A Follow that a peer refuses outright is
  shown as "Could not deliver" and is retried after a week (`69d8893a`,
  `14c8ab8c`).
- **Moderator edits of a local community** are relayed as the community's own
  Update, carrying only what was applied (`22bac5ab`, `83e12372`).
- **Capped responses.** A federation delivery reads at most a capped response
  body, and a delivery that cannot be sent is not requeued (`2176e0b8`,
  `beb7ba64`).

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

Since the register closed:

- **Signatures.** RFC 9421 signatures are accepted inbound (`cb3cee2f`). For
  LD signatures:
  - every signature option is hashed, as Mastodon does (`0aee04f0`);
  - an expired signature is refused (`979dad1e`);
  - an LD signature is honoured only for documents whose JSON it actually
    covers (`d421395a`);
  - an LD-accepted activity takes its content from the origin (`c2ddc4a8`);
  - the standard JSON-LD contexts are served locally, and a context named by
    an incoming document is never fetched (`371934b9`, `02dc4c10`).

  A signature whose keyId names another host is refused (`7c60cd55`). A
  signer's key is refetched once when a new key could cure the failure
  (`dd189271`, `be3e5d46`). Signatures that leave required headers unsigned
  are logged, or refused under `SIG_REQUIRED_HEADERS_ENFORCE` (`a823ca72`).
- **Dedupe.** An activity id is remembered only after its signature verifies
  and its hand-off succeeds, and dedupe keys are namespaced by actor host
  (`3a6cb73a`, `dd7a834e`, `3738574f`).
- **Announced activities.**
  - An Announced Create or Update is always refetched and must match the id
    and author it names (`be60b179`).
  - An Announced activity from another host must name an actor on that host
    (`005e0aeb`).
  - A cross-origin Announced Delete needs a community moderator and waits
    `cross_origin_delete_delay` unless the origin confirms the deletion
    (`4bab1542`, `f6d893eb`).
- **Trust of hosts and inboxes.**
  - A remote actor's or instance's inbox is trusted only on its own host
    (`5a328fa3`, `4729e00f`).
  - An actor found by WebFinger is stored under the handle its own server
    confirms (`c60fca0b`, `7804f327`).
  - A lookalike host is no longer taken for this instance (`72cf2209`).
- **New activity handling.**
  - A remote actor's Update of itself refreshes its profile, key and inbox
    (`cd8d7423`).
  - An Undo of an EmojiReact takes the reaction back (`0b474273`).
  - An Undo of a Flag withdraws the report while moderators have not acted on
    it (`eb53e6da`).
  - An Undo of an Add or Remove reverses the sticky, moderator or feed change
    (`5c25588c`).
  - A Group that deletes itself becomes read-only (`cc07742c`).
  - A federated Remove may not leave a local community with no moderators
    (`2e90451c`, `2693318b`).
- **New object types.** An activity with an array-valued `type` is dispatched
  (`be0b95bd`). A Castopod Podcast actor is accepted as an author
  (`e2a35b2b`). An Application actor is a bot user unless it stands for a
  whole server (`5186c9e6`). A Create of `Audio`, `Image`, `Document` or
  `Video` becomes a post (`7b967d8f`). `quote` ids and `contentMap` language
  variants are stored (`47906e0f`, `9d6a7d02`).
- **Relayed content.** A relay's Announces are stored as relayed posts.
  When a forward from an accepted relay has no verifiable LD signature, the
  object is fetched from its origin instead of being refused (`61edf3e7`,
  `9cbba55b`).
- **Fixes.** `process_delete_request` is a Celery task again, so a remote
  account deletion no longer crashes `/inbox` (`d3582127`). Create and
  Announce ids up to 255 characters are kept (`1d2c7bdc`). A peer's published
  date is clamped to now (`dfb15360`).

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

### 3.1 Since the register closed

The ActivityPub security track and the fuzzing track found the following. All
were committed as `fix:`.

**Forged or replayed federation**
- **LD signatures.**
  - An expired signature was accepted, and its expiry was not read from what
    was signed (`979dad1e`, `4a460df1`).
  - A signature was honoured for JSON it did not cover (`d421395a`).
  - An LD-signed actor Update could replace a key without a fetch
    (`22e028d9`).
- **Announces.** A cross-origin Announce could inject a Create, Update or
  Delete for another server's content (`32113d7b`, `be60b179`, `4bab1542`).
- **Signer identity.**
  - A signature made for another instance verified here (`313fc6a8`).
  - A keyId could name a different host from its actor (`7c60cd55`).
  - An unsigned ChatMessage could claim the fediseer actor (`2a10cc8f`).
- **Stored inboxes.** A remote actor or instance could point its stored inbox
  at another host (`5a328fa3`, `4729e00f`).
- **Local communities.** A moderator's Update of a local community could
  replace its key or ids (`efcf2808`).

**SSRF and resource exhaustion**
- A JSON-LD context named by an incoming document was fetched (`02dc4c10`).
- Outbound GETs and POST responses had no size or time cap. Every fetch is now
  capped at the transport, with compressed bodies counted and decoded within
  the cap (`3c2adf1f` to `945da950`).
- LD verification normalised arbitrarily nested JSON-LD, whose cost is cubic
  in the nesting. Nesting past 32 levels is now refused (`ba17ad9c`).
- Fifteen markdown and HTML sanitiser regexes backtracked catastrophically on
  unclosed input, a ReDoS. Each was fixed to run in linear time (`534e6183` to
  `bf3dd349`).

**Information disclosure**
- `/activities` served any logged activity to anyone (`ec22836d`).
- Followers-only content leaked through moderator acknowledgements, the
  modlog, notifications, cross-posts and the purge page (`29617f0b`,
  `02a791ac`, `9000bfeb`, `334cfb9b`).
- `.env` files, with their secrets, were copied into the Docker image
  (`9fc24049`).

**Other**
- A crafted Signature header 500'd a public ActivityPub GET (`425b5b2d`).
- `is_local()` took a lookalike host for this instance (`72cf2209`).
- A peer URL whose browser-visible host differed from the host the ban check
  read could pass a domain ban (`bfb1a32e`, `67daf49a`).
- A comment's translation was swapped into the page unsanitised (`60780390`).
- The actor-link filters linked handles inside links and code spans
  (`27dfc34b`).

---

## 4. Other fixes by area

The 1,103 `fix` commits are summarised here by theme. The counts below are
by the application module each commit changed most, and cover the fix,
security, feat and perf commits together:

| Module | Commits |
|---|---|
| `app/activitypub` | 431 |
| `app/shared` (shared actions and background tasks) | 160 |
| `app/utils.py` | 98 |
| `app/discovery` | 93 |
| `app/models.py` | 64 |
| `app/api` | 56 |
| `app/community` | 55 |
| templates | 39 |
| `app/post` | 29 |
| `app/admin` | 24 |
| `app/user`, `app/relays`, `app/main` | 22 each |
| `app/feed` | 20 |
| `app/auth` | 15 |
| smaller modules | the rest |

Another 37 touched only tests or tooling.

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

### Fixes in the feature tracks

The feature tracks after 2026-10-01 were built test-first, so most of their
`fix` commits correct a new feature during review. These are listed with
their features in 2.3 and 2.5. Fixes to code that predates the tracks:

- **Request-path sleeps.** Resolvers, inbox processing and relay detection no
  longer sleep and retry in-line. Only housekeeping tasks retry (`e2a9f1fd`,
  `970cf43b`, `fc69633d`, `ce63a354`). Background image processing keeps its
  retry (`5471a860`).
- **Celery.** Background work runs in its own worker, so it can never starve
  the inbox (`261ee07b`). Each task starts without the previous task's
  `g.site` (`4a96883b`). The sync tasks use the background queue
  (`4ee9a711`).
- **Backfill.** An entry that cannot be stored is skipped instead of ending
  the whole backfill (`8a049a57`). A community with no outbox still gets its
  featured posts and reply counts (`e1d4a1f4`).
- **Nodeinfo.** A host whose nodeinfo fails is asked once a day, with a
  5-second cap. A nodeinfo 2.x href is followed only when it is https on the
  instance's own host (`5a25a37a`, `f3d70a2f`).
- **Translations.** Stale fuzzy suggestions whose placeholders belong to
  another string were cleared (`fbe25207`, `0348e87f`).
- **`fastapi_server`** starts and stops with a lifespan handler, not
  `@app.on_event` (`28bf4510`).

### Fuzzing track

Atheris harnesses now cover the input surfaces in tiers: signatures and
transport, ActivityPub JSON, and markdown with ReDoS checks so far. Each
finding was fixed test-first, one commit per defect. Typical fixes:

- malformed Signature, Content-Length and Accept headers are a format error
  rather than an exception (`959a73d1`, `ce9f14cd`, `28803316`, `3a0c91fc`);
- out-of-range dates are refused (`df078aef`, `f5769345`, `8db5c76c`);
- a shared-inbox body that is not a JSON object is refused (`8446c1a1`);
- a host longer than 253 characters is refused before IDNA (`fd18766d`);
- a gallery reads at most 20 attachment entries (`a2cf04a8`);
- markdown nested past markdown2's recursion limit renders nothing
  (`e5c030c5`);
- the linear-time regex rewrites listed in 3.1.

---

## 5. New features

There are 146 `feat` commits. The first twelve came during the coverage
campaign:

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

The other 134 came from the feature tracks after 2026-10-01. Their behaviour
is described in 2.3 to 2.5:

| Track | Spec | Features |
|---|---|---|
| Visibility | `2026-10-01-activitypub-interop-design.md` | Followers-only posts and replies, stored, enforced, accepted and created locally; hidden-reply placeholders; signed-fetch entitlement |
| Rich content | same | Content warnings, multi-image galleries, sensitive-image blur, Castopod episode audio and covers |
| Discovery (D24) | `2026-10-03-discovery-seeding-design.md`, `2026-10-05-proactive-discovery-sync-design.md` | Directory refresh, search fallback, podcast communities and credits, proactive sync, Media tab, wider-network video search |
| Live | `2026-10-07-microblog-live-feed-design.md` | Live view on microblogs and home tabs, `/live/stream` |
| Relays | `2026-10-08-activitypub-relays-design.md`, `2026-10-13-relay-forward-origin-fetch-design.md` | Relay subscriptions, relayed-post storage and expiry, origin fetch of unverifiable forwards |
| Server views | `2026-10-08-microblog-instance-views-design.md`, `2026-10-08-server-views-in-community-list-design.md`, `2026-10-13-server-view-join-design.md` | `/c/microblogs@<host>`, listing in `/communities`, joining |
| Security | `2026-10-10-ap-security-track-design.md` | RFC 9421 inbound and double-knock, key refetch, secure mode, followers-only posts in local microblogs |
| Interop C1 | `2026-10-11-ap-interop-c1-design.md` | JSON-LD content type, actor Update, Undo of EmojiReact, Flag, Add and Remove, Group self-Delete, Person collections, user outbox, `rel="alternate"` links, WebFinger-confirmed handles |
| Interop C2 to C5 | `2026-10-12-ap-interop-c2-quote-posts-design.md` and C3 to C5 | Quote posts on input, multi-language variants, media-type objects, count-only likes, shares, liked and following collections |
| Interface | | Italian, Korean and Russian translations offered (`122ee9f7`) |

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
- The test database runs with `jit=off`. The suite's tables are never
  analysed, so the planner JIT-compiled small queries at about 2.7 s each
  (`compose.test.yaml`).
- `run_tests.sh` also runs the client-side tests for `live_feed.js` with node
  when it is installed, and fails below 100% coverage.
- `tests/fuzz/` holds the atheris harnesses, a smoke campaign
  (`tests/fuzz/smoke.py`) and corpora replayed by the suite with a per-input
  time budget for ReDoS.
- The suite collects 22,420 tests.

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
- **`SIG_REQUIRED_HEADERS_ENFORCE` defaults to off.** The code default stays
  `0`, so that an operator can review the logged warnings before refusing
  under-signed requests.
- **Domain bans match one spelling only.** A ban on `bücher.example` does not
  match `xn--bcher-kva.example`, or the reverse.
- **Fuzzing is in progress.** Tiers for URLs and feeds, NNTP, API and imports,
  and image decoding are planned but not yet run.
- **Converted form buttons are not browser-verified.** The Join, Leave,
  notification-bell and feed buttons that changed from links to POST forms
  are covered by tests but have not been checked by hand in a browser.
  Reviewers should exercise them, with and without JavaScript, before
  release.

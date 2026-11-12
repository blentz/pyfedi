# Changes since v1.8.10

This document summarises what changed on the `blentz` branch between the tag
`v1.8.10` and the current HEAD. It is written for two audiences: instance
operators who are upgrading, and PieFed developers reviewing the fork. The
upgrade notes come first because they are what an operator must act on.

The branch was started on `v1.7.17` and rebased onto `v1.8.10` on 2026-10-31.
Section 1.1 records what the rebase changed; everything else describes the
branch's own work, which is the same as before the rebase except where 1.1
says otherwise.

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

After the register closed (2026-10-01 to 2026-10-14), work moved to feature
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

This summary covers the branch up to the end of the fuzzing track
(2026-10-14). That part of the range contains 3,248 commits on top of
`v1.8.10`:

| Type | Commits |
|---|---|
| `fix` | 1,178 |
| `test` | 1,099 |
| `docs` | 702 |
| `feat` | 146 |
| `refactor` | 48 |
| `chore` | 39 |
| `security` | 12 |
| `perf` | 6 |
| other (`revert`, `build`, `deploy`, untyped) | 18 |

Before the rebase the same work was 3,250 commits on `v1.7.17`; the two that
dropped out are listed in section 1.1. The branch has since grown to
4,478 commits. Of the tracks after 2026-10-14, roles and permissions, payments,
the media cap and the client-app APIs are summarised here; onion storefronts and
multi-domain storefronts, Pixelfed and Loops interop and referral-code
registration are not yet.

The client-app APIs (2026-11-07 to 2026-11-12, 233 commits after the payments
track) follow `docs/superpowers/plans/2026-11-07-api-completeness.md`, whose goal
is to serve as many fediverse client apps as PieFed can. A harness under
`interop/apps/` drives 21 third-party apps against a PieFed stack, on an Android
emulator and in browsers, and each phase was merged only when the apps it serves
logged in, read and posted there:

- An OAuth 2 authorization server for client apps (W0.2), and a shared read layer
  under `/api/alpha` that every new API reads through (W0.1).
- Mastodon's client API (W1), with Pixelfed's own routes for its official app,
  PixelDroid and Fedilab (W1.4), including direct messages and stories.
- PeerTube's API for NewPipe and PeerTube's own app, uploads included (W2).
- Lemmy's `/api/v3` for Jerboa (W3).
- gpodder.net sync for podcast apps (W4).

The test suite collected 23,526 tests on 2026-10-14, 29,440 after the
rebase, and 33,001 on 2026-11-12.

### 1.1 The rebase onto v1.8.10

On 2026-10-31 the branch was moved from `v1.7.17` onto `v1.8.10`: its 4,143
commits were replayed one by one on top of the 395 that v1.8.10 adds, and every
conflict was resolved in the commit that met it. Every commit hash in this
document is the rebased one, except the three dropped commits named below and
the old tip: the branch as it stood before is kept as
`backup/blentz-pre-v1.8.10` (tip `77df00a8`), where those four are.

**What dropped out.** Three commits became empty and were dropped:
`3b0dd026` (upstream's own "ensure rss bot can post in mod-only communities",
already in v1.8.10), `ddc41f65` (an RSS crash on an author with no
`ap_public_url`, which v1.8.10's RSS rewrite had already removed), and
`df75c6a5` (a second copy of "init-db makes the default SiteDomain row", whose
first copy survives).

**What v1.8.10 brings that this branch now has.** Gallery posts (several
uploaded images, a composite thumbnail); reposter accounts (`User.reposter`,
`Post.from_reposter`, a per-user filter, and an unanswered bot challenge now
flags a reposter); the post approval queue (`ban_posts` holds an account's
posts for review instead of refusing them); a statistics page and per-instance
active-user counts; instance liability and defederation flags; a community's
default hashtag and posting-warning override; scheduled posts in the API; the
"Following / Everyone" choice on the microblogs community; the instance chooser
fed from the main instance list instead of fediverse.observer; one `RSSFeed`
class behind every RSS feed (items carry `media:content`, newest first, cached
by posting rate, channel language dropped); a votes "spicy score" that
amplifies early votes in the ranking only, the stored score counting each vote
once; native AVIF in Pillow, so `pillow-avif-plugin` is gone.

**Where the branch's work was moved onto v1.8.10's.** Where v1.8.10 rewrote
code the branch had changed, the branch's change was applied to the new code
rather than restoring the old:

- RSS: the content-warning, podcast (Podcasting 2.0), storefront, branding and
  XML-safety changes are now in `app/rss_extras.py`'s `RSSFeed`, which gained
  `language=` and `podcast=` arguments, and each feed route builds its links on
  the visitor's storefront. The domain feed still gives one entry per URL.
- `update_post_from_activity` keeps v1.8.10's `try ... except
  LockNotOwnedError` wrapper and 120 s lock; the body is the branch's. A reply's
  Update lock takes the same timeouts (D249's ruling).
- Community deletion runs in v1.8.10's `delete_community_task`, which also
  forgets a synced community (interop D24).
- `get_emoji_replacements`, the instance chooser, the community feed, the
  community and user actor documents and the API's `user_view` took v1.8.10's
  new fields and filters on top of the branch's fixes.
- v1.8.10's gallery uploads go through the branch's `store_post_image`, so they
  get the type check, metadata stripping, SVG refusal, blocked-image check and
  the uploader's domain; the composite thumbnail is stored on the post's
  domain.
- **One multi-image post: v1.8.10's gallery** (owner ruling 2026-11-01, spec
  `docs/superpowers/specs/2026-11-01-gallery-type-design.md`, G-1 to G-8). A
  post with two or more pictures is a gallery post (type 7) everywhere: an
  incoming Pixelfed or Mastodon album, the web form and the API. The branch's
  album (an image post whose other pictures were its gallery) is gone, and its
  features moved onto the gallery: every picture federates in order as a
  Document (a WebP, AVIF or HEIC as its JPEG), the Page form adding v1.8.10's
  Link to the post; the Galleries filters, blur, content warning, teaser count
  badge, RSS `media:content` and the API's `extensions.gallery` take gallery
  posts. The web gallery form offers one slot per picture the site allows
  (`max_album_images`, default 4), each checked as an image post's is (size
  cap, chan filter) and stored by `store_post_image`; the image form takes one
  picture. The API's `gallery` is every picture, two to `max_album_images`,
  and makes the post a gallery. Migration `c3e9a7d1f5b2` converts existing
  album posts (section 2.1).

**Upstream changes kept out, or corrected.**

- *`Post.vote` and `PostReply.vote`* refuse a downvote from anyone the author
  has blocked, local author or not; v1.8.10 checks local authors only.
- *`index_rss`* stays uncached, so a private instance's check runs on every
  request; v1.8.10 re-enabled `@cache.cached`.
- *`retrieve_metadata_of_url`* stays the branch's single capped, non-redirecting
  fetch with the `<title>` fallback, now also returning `og:image`; v1.8.10's
  version returned two values on one path and three on the other.
- *An audio `Document`* (a PieFed podcast episode) is still a link post to its
  audio; v1.8.10 counted it as an image.
- *Function-level imports, `Query.get` and tuple form choices* that v1.8.10's
  new code uses were brought to the branch's rules.

**Bugs in v1.8.10 fixed after the rebase** (`9cd67edc`, after `d7e52f2c` restored the two functions the replay broke and `089750fb` dropped the `pillow_avif` imports):

- Deleting a community called `delete_community_task()` without the community
  id, and the task read `current_user`, which a Celery worker does not have.
- `/admin/activity_json` wrote a valid JSON document into the page unescaped.
- A post preview with no `type` was a 500.
- An unreadable or future `published` on an incoming post or reply was not
  caught (`ap_parse_datetime`), so the object was refused; it is now read with
  `parse_ap_published`, as the rest of the branch does.
- A nodeinfo document without `usage`, and a Lemmy site document without
  `site_view.local_site`, raised and rolled back the rest of the instance's
  refresh.
- The instance chooser raised on a list that was not a list of nodes, ending
  daily maintenance.
- Seven admin routes sent an anonymous visitor to "permission denied" instead
  of the login page (decorator order).
- The teaser's Join link posted to a GET-only URL.
- The Markdown preprocessors rewrote the inside of an HTML `<code>` block.
- An image a `ban_posts` account uploaded as media was published when it was
  ready; it is now held for review like the account's other posts.
- `tests/test_gallery_thumbnail.py` read sample photographs that are not in the
  repository, after a `chdir` out of it.

Found while covering v1.8.10's code to the floors, each fixed test-first:

- The gallery form's picture slots each demanded a CSRF token the page never
  rendered, so with CSRF on (as in production) no gallery ever validated;
  v1.8.10's `validate()` returned True whatever the fields said, which hid it
  (`2647d5df`). A gallery post's url was never stored either.
- `build_instance_list` compared versions as text, so every PieFed past 1.9
  was left out of `known_instances.json`, and one malformed entry from a peer
  stopped the list being written (`01cc77c7`).
- `/statistics` listed the onion's instance row on the clearnet, linked its
  pages to the moderation log (or to `/statistics/?page=2`), and sent a
  second, public Cache-Control on a signed-in reader's page (`2c4a355c`).
- The admin voting-pattern, downvote and post-timing views answered an
  unknown actor with an empty page or a 500, and on the clearnet showed an
  onion account and the onion authors it downvoted (`49b02c65`).
- Approving a held post re-published and re-federated a post that was
  already published or deleted, and was a 500 for a permission holder with no
  say over the community; the inline comment edit dropped its redirect and
  skipped the edit page's visibility and post checks (`f5992942`).
- A local gallery's thumbnail fetched our own uploads over HTTP (`0d7b9750`).
- A scheduled post whose author had no timezone failed through the API and
  was never published by `publish-scheduled-posts` (`900cee40`).

**Database.** v1.8.10 adds seven migrations from the same revision the
branch's start from; `b8d1e6f4` merges the two chains (section 2.1).

**Decisions taken after the rebase** (owner, 2026-11-01).

1. An incoming multi-image post is v1.8.10's gallery post, and so is a local
   one: the branch's album and v1.8.10's gallery upload are one feature (above).
2. The author-blocked-instance 401 on `post_ap`, `comment_ap`, their
   collections and the user outbox is dropped, as v1.8.10 dropped it
   (`449ffac0`), with its `Vary: User-Agent, Signature`. A public object was
   never kept from a blocked instance by it: an unsigned fetch, another
   User-Agent or a third party got round it, and the `Vary` kept every
   blocking author's objects out of shared caches. An author's instance block
   still refuses that instance's votes, replies and deliveries.

---

## 2. Upgrade notes

### 2.1 Database migrations

The branch's migrations start from `7b8bf43fa079`, the last migration of
v1.7.17. v1.8.10 added seven migrations of its own from that same revision
(instance liability and statistics, reposters, the community default hashtag
and posting-warning override, the nodeinfo index), so after the rebase there
are two chains. The merge migration `b8d1e6f4a2c7` joins them and is the
single head. None of the seven touches a column or index the branch touches.

- **From v1.8.10:** `flask db upgrade` applies the branch's chain, then the
  merge migration.
- **From a `blentz` deployment made before the rebase** (database at
  `a7c2e5f9d3b1`): `flask db upgrade` applies v1.8.10's seven migrations,
  then the merge migration. Nothing of the branch's runs again.
- **Then, either way:** `c3e9a7d1f5b2` album_posts_become_galleries turns
  every image post with an album (`gallery_count > 0`) into a gallery post. A
  copy of the image's File (url, alt text, size, hash and paths, flagged as
  not written by us, so it never removes the image's files) becomes the
  picture at weight 0, ahead of the album's weights 1..; `gallery_count`
  counts it; the image stays as the thumbnail and `post.url` keeps the first
  picture. Rewrites data: those posts only. The downgrade reverses exactly the
  posts it converted.

The table lists the branch's first twenty-six migrations, which cover the
work this summary describes, in the order they apply. The branch has added
more since. Several of them rewrite data. Only the last in the table deletes
rows: it merges rows that name one IPv6 address in different spellings.

The NOT NULL migrations among the first four use a lock-friendly recipe: each adds a
`CHECK (col IS NOT NULL) NOT VALID` constraint, validates it (which scans the
table but only takes a `SHARE UPDATE EXCLUSIVE` lock, so reads and writes
continue), then sets NOT NULL without a second scan and drops the constraint.
This needs PostgreSQL 12 or later to avoid a full-table `ACCESS EXCLUSIVE`
scan. The backfill `UPDATE`s touch only rows that are actually NULL.

| Order | Revision | What it does | Rewrites data? |
|---|---|---|---|
| 1 | `9e99070afe06` backfill_reply_count_cross_posted | Fills `post.reply_count_cross_posted` from `reply_count` where it is NULL, and gives the column a server default of 0. Several code paths did `None - 1` on old rows. (`f3edf04e`) | Yes, NULL rows only |
| 2 | `c4f1a9d7e2b8` post_sort_columns_not_null | Backfills and makes NOT NULL the seven columns the post sorts page by: `sticky`, `instance_sticky`, `score`, `ranking`, `ranking_scaled`, `posted_at`, `last_active`. `posted_at` falls back to `created_at`; `last_active` falls back to the newest reply's time, then `posted_at`. Adds matching server defaults. A NULL in any of these made a post silently missing from every page of that sort. (`43a98a7f`) | Yes, NULL rows only. This is the largest migration: it scans `post` once per column |
| 3 | `1812a0b161d5` user_verified_not_null | Sets `user.verified = false` where NULL, adds a server default, and makes the column NOT NULL. A NULL user had been let through guards as if verified (D645). (`7cec275d`) | Yes, NULL rows only |
| 4 | `05ae68b2ebb9` user_unread_notifications_not_null | Sets `user.unread_notifications = 0` where NULL, adds a server default, makes it NOT NULL. Incrementing a NULL count raised `TypeError` (D274). (`f1c2a0dc`) | Yes, NULL rows only |
| 5 | `9c3e5a1d7b20` passkey_counter_reset | Sets every `passkey.counter` to 0. The stored values were invented by the old code rather than reported by authenticators; real counts are recorded from each passkey's next login (D886, D887). (`0e4f61fc`) | Yes, every passkey row. Not reversible |
| 6 | `3f8b2c6d9e41` event_more_info_url | Adds a nullable `event.more_info_url` column (R223). (`d8f7a9f5`) | No |
| 7 | `23d65cdb8207` pending_follows_are_null | In `user_follower`, outward follows stored as `is_accepted = false` that still have a matching follow request are reset to NULL (pending). Rows with no request left are genuine refusals and stay `false` (R265). (`9d488574`) | Yes, matching rows only |
| 8 | `8c1d4e7f2a90` quote_authorization | Creates the `quote_authorization` table (post, reply, quoting URI, approval time), used by `/quote_boost_auth` (R205). (`35abcf19`) | No |
| 9 | `a7e3f1c2b9d4` visibility | Adds `visibility` (NOT NULL, default `public`) to `post` and `post_reply`, each with an index. The column is instant on PostgreSQL 11+, but the indexes are built with a plain `CREATE INDEX`, which blocks writes to each table while it builds. (`5330d53c`) | No |
| 10 | `b5d2c8e1f7a3` content_warning | Adds `content_warning` to `post` and `post_reply`, and `post.gallery_count` (default 0). (`19d2d43b`, `16a784c6`) | No |
| 11 | `c6e1d9a4b7f2` discovery_entry_and_post_extensions | Creates `discovery_entry` and adds the JSON `post.extensions`. (`e90eee65`) | No |
| 12 | `d7f3a9c2e4b1` ap_activity_ids_255 | Widens `ap_create_id` and `ap_announce_id` on `post` and `post_reply` from 100 to 255 characters. (`49f3bf17`) | No |
| 13 | `e8a4c1f9d2b6` discovery_sync | Creates `discovery_sync`, the communities the instance actor keeps synced. (`0a53700f`) | No |
| 14 | `f1b7d3a8c5e2` discovery_exclusion | Creates `discovery_exclusion`, actor URLs the sync must never re-add. (`d996fc73`) | No |
| 15 | `a3c9e5f1b7d4` rerank_backfilled_posts | Recomputes `ranking` (and shifts `ranking_scaled` by the same amount) for every post whose `posted_at` is more than an hour before `created_at`, so that backfilled posts sort by when they were published. (`9cb75f3f`) | Yes, those rows. Not reversible |
| 16 | `b7e1c2d9f4a6` relays | Creates `relay` and adds a nullable `post.relay_id`. The foreign key is added `NOT VALID` and the index is built `CONCURRENTLY`, so `post` is never locked for a scan. (`95959445`, `1cb4a5a7`) | No |
| 17 | `c4a8e2f61d3b` instance_signature_format | Adds `instance.signature_format` (default `cavage`), which records whether a peer takes RFC 9421 signatures. (`c0219bec`) | No |
| 18 | `e5c1d8a2b7f9` strip_non_open_cross_posts | Removes the ids of followers-only posts from every `post.cross_posts` array, clears a followers-only post's own list, and recounts. No row is deleted. (`98a30ad3`) | Yes, matching rows. Not reversible |
| 19 | `d9f3b7a15c2e` site_require_signed_fetch | Adds `site.require_signed_fetch` (default false), the secure-mode switch. (`89cad496`) | No |
| 20 | `e2a7c4f9d1b3` report_ap_id | Adds `report.ap_id`, so an Undo of a Flag can find its report. (`47f461b2`) | No |
| 21 | `f4b8d2a6c1e7` user_outbox_indexes | Builds `(user_id, posted_at DESC NULLS LAST)` partial indexes on `post` and `post_reply` `CONCURRENTLY`. A rerun after an interruption rebuilds an invalid leftover. Needs Alembic 1.12 or later, now pinned. (`2d856085`, `64a89627`, `3016d564`) | No |
| 22 | `a7c3e9f1b5d2` quote_ap_id | Adds `quote_ap_id` to `post` and `post_reply`. (`a5651338`) | No |
| 23 | `b8e2d4f6a1c3` language_variants | Adds the JSONB `language_variants` to `post` and `post_reply`. (`521bfe91`) | No |
| 24 | `c9f3a5b7d2e4` vote_federated_like | Adds the nullable `federated_like` to `post_vote` and `post_reply_vote`. (`097ff04f`, `e9c160e9`) | No |
| 25 | `d4a8e2c6f1b9` server_view_member | Creates `server_view_member` (user, instance), the joins of microblog server views. (`2e12b601`) | No |
| 26 | `3d52b8a19cf5` ipv6_canonical_hosts | Rewrites IPv6 hosts in `domain`, `banned_instances` and `allowed_instances` to one canonical spelling (`[0:0::1]` becomes `::1`). `domain` rows that name the same address are merged into the oldest or already-canonical row: flags are OR-ed, `post_count` is summed, a posting warning is kept, and every foreign key to `domain.id` (found from the catalogue: `post`, `post_reply`, `scheduled_post`, `domain_block`) is repointed before the duplicates are deleted. A user's block of a merged row is dropped when that user already blocks the survivor. A repeated `banned_instances` or `allowed_instances` row is deleted. Hosts without a `:` are untouched. (`bf837e71`) | Yes, IPv6 host rows only. Deletes duplicates. Not reversible |
| 27 | `b4e8d2a6f1c9` role_grants | Replaces `user_role` with `role_grant` (user, role, optional domain, granted by, granted at, optional expiry), adds `role.built_in`, `role.listed_publicly`, `role.description` and `mod_log.site_domain_id`, deletes the roles "Anonymous user" and "Authenticated user", which conferred nothing, and marks the role named Admin as the built-in one. (`b0056476`) | Yes. The downgrade drops domain grants |
| 28 | `c5f9e3b7a2d1` split_permissions | Moves every role onto the finer permission catalogue so that each keeps what it could do: each old permission's rows become the permissions split from it, and the role named Staff gains what its name used to give it. Removes `change user roles`. (`ec35df1e`) | Yes, `role_permission` rows. The downgrade does not restore `change user roles` |
| 29 | `d6a1f4c8e2b3` permission_scope | Adds `role_permission.all_domains` and `role_permission_domain`, and drops `role_grant.site_domain_id`: a role granted on a domain becomes its own role limited to that domain. The role named Staff gains `administer all users` and its scopable permissions are limited to the default domain. `site_domain.registration` values `staff` and `admin` become `closed`. | Yes. The downgrade restores Staff's reach and drops other limited permissions rather than widen them |
| 30 | `e4c8a1d7f3b6` split_moderation_permissions | Gives every role holding `moderate all communities` the new `remove and restore content` and `turn comments off`, at the same domains, so nobody loses anything. The downgrade drops both. |
| 31 | `a7d3f1c9e5b2` cms_nav_links_and_donate_page | Adds `in_header`, `in_footer` and `show_sidebar` to `cms_page`, and seeds the `/donate` page, or turns all three on for an existing one. (`aa71e0c2`) | Yes, an existing `/donate` page's three flags |
| 32 | `b2e6c4a8d1f3` payments | Creates `payment_provider`, `offer`, `offer_domain`, `member_subscription`, `payment` and `payment_event`, and adds `user.donor_until`. (`3c9ba6bc`) | No |
| 33 | `c3f7a9e1d5b8` payment_link_offers | Adds `offer.url`, an offer that links elsewhere. (`0e58d103`) | No |
| 34 | `d4a8b2f6c1e9` paid_private_domains | Adds `site_domain.login_required`, `registration_offer_id` and `lapse_grace_days`, `member_subscription.ends_at`, and `user.registration_pending_since` and `access_lapsed_at`. Every domain but the default takes `login_required` from the site's `private_instance`. (`94917dfa`) | Yes, `site_domain.login_required` |
| 35 | `e5b9c3a7d2f4` prepaid_reminders | Adds `member_subscription.reminded_at`. (`ca555b17`) | No |
| 36 | `f6c2a8e4b1d7` oauth_server | Creates `oauth_app`, `oauth_code` and `oauth_token`. Secrets and tokens are stored as SHA-256 digests. (`bd9e7096`) | No |
| 37 | `a7d3e9b2c4f1` oauth_domain | Adds `domain` to `oauth_code` and `oauth_token`: a code or token is found only on the domain it was issued on. Existing rows take the default domain. (`ec88a532`) | Yes, existing codes and tokens |
| 38 | `b8e4f0c3d5a2` user_mute | Creates `user_mute` (muter, muted, whether notifications too, until when). (`32948b64`) | No |
| 39 | `c9f5a1d4e6b3` push_subscription | Creates `push_subscription`, a Web Push subscription per token. (`8aa72e3c`) | No |
| 40 | `d1a6b2e7f4c8` video_upload | Creates `video_upload`, a video arriving in parts through PeerTube's resumable upload. (`88bac4d8`) | No |
| 41 | `e2b7c3f9a1d5` conversation_member_state | Adds `state` (`active` or `request`), `hidden` and `muted` to `conversation_member`. Existing members are active, shown and unmuted. (`e84dc5fb`) | No |
| 42 | `f3c8d4a2b6e9` chat_message_file | Creates `chat_message_file`, a message's pictures in order. (`a22c9518`) | No |
| 43 | `a4d9e1c7b3f2` story_view_and_expiry | Creates `story_view` (who saw a story) and `story_expiry` (a story its author ended early). (`c9aadf19`) | No |
| 44 | `b7e2c9d4f1a3` gpodder_devices_and_subscriptions | Creates `gpodder_device` and `gpodder_subscription`. (`35f5053a`) | No |
| 45 | `c3f8a1e6d2b9` gpodder_episode_actions | Creates `gpodder_episode_action`. (`03f0b400`) | No |

Downgrades remove defaults, constraints, columns and the new tables, but do
not restore the NULLs, invented counters, rankings, cross-post links or
merged host rows that the upgrades replaced; there is no record of which rows
held them. The downgrade of `3d52b8a19cf5` does nothing.

### 2.2 Configuration

New or changed settings in `env.sample`, `env.docker.sample` and `config.py`:

| Setting | Default | Effect |
|---|---|---|
| `TRUSTED_CLIENT_IP_HEADER` | empty | Where the client IP is read from for rate limiting, IP bans, the honeypot ban, geolocation and the `user.ip_address` audit column. Empty means `request.remote_addr`, which `ProxyFix(x_for=1)` already takes from the last `X-Forwarded-For` entry your proxy appended. **Behind a single Caddy or nginx proxy, leave it empty.** Behind Cloudflare or a similar CDN, set it to the CDN's header (for example `CF-Connecting-IP`), and only if clients cannot reach PieFed directly. If the header holds a list, the last entry is used. Previously PieFed read `CF-Connecting-IP` and then the *first* `X-Forwarded-For` entry, which the client controls. (`6acda199`) |
| `WEBHOOK_SECRET` | empty | Shared secret for the plugin `/webhook` endpoint. Empty disables the endpoint (it answers 404). When set, a request must carry it in an `X-Webhook-Secret` header (compared in constant time) or it is refused with 403. **If you use a webhook plugin, set this and configure the sender, or the plugin stops receiving calls.** `docs/PLUGINS.md` is updated to match. (R223, `5a912e27`) |
| `MAX_CONTENT_LENGTH` | `104857600` (100 MB) | Largest request body accepted, in bytes. Larger requests are refused with 413 before the body is read (a rendered page on the web, JSON under `/api/`). Image uploads are separately capped at 10 MB, so raise this only if you allow large video uploads. (`82301ada`) |
| `SKIP_RATE_LIMIT_IPS` | `127.0.0.1` | Parsing changed. It is now always a list: a comma-separated string from the environment is split and trimmed. Previously, once set from the environment it was a plain string and the membership test was a *substring* test, so `192.168.1.10` also exempted `92.168.1.1` and `2.168.1.1` from the login rate limits (D1396, `0228d697`). Existing comma-separated values keep working. |
| `redirect_policy` (admin setting, not `.env`) | `same_origin` | Stored in the `Setting` table and chosen on the admin Misc page. Controls which hosts `?next=` and `Referer` redirects may go to: `same_origin` (this server only), `trusted_servers`, `federated_servers` (known, not banned, not gone) or `all_referrers` (labelled dangerous). The default keeps the new strict behaviour. URL parsing rules do not change with the policy. (`5f531bf7`) |
| `SIG_REQUIRED_HEADERS_ENFORCE` | `0` | When `1`, an inbound HTTP signature that does not cover `(request-target)`, `host`, `date` and (on a POST) `digest` is refused. At `0` the gap is only logged as a warning. Review the warnings before turning it on. It is read in `config.py` but not listed in `env.sample`. (`d3e67c06`) |
| `CELERY_QUEUES`, `CELERY_CONCURRENCY`, `CELERY_PREFETCH_MULTIPLIER`, `CELERY_MAX_TASKS_PER_CHILD` | `celery,send`, `4`, `1`, `1000` | Read by `entrypoint_celery.sh`. **The default worker no longer consumes the `background` queue.** See 2.2.1. (`0346d103`, `05ec16c6`) |
| `LANGUAGES` (`config.py`) | | The interface now also offers Basque, Finnish, Hindi, Italian, Korean, Polish and Russian (`15dc82fc`). |
| `require_signed_fetch` (admin Federation page) | off | Secure mode. ActivityPub GETs must be signed by an actor this instance already knows. Unsigned fetches of user, community and feed actors get a minimal document (inbox and key only); everything else is refused. Signed answers are kept out of shared caches. (`89cad496`, `fafdac03`, `7705d2f1`) |
| `cross_origin_delete_delay` (admin Federation page) | 90 seconds | How long a moderator's Announced Delete of a post from another server waits before it is applied, unless the origin confirms the deletion first (`59a6cbec`). |
| `relay_retention_days` (admin Relays page) | 7 | Relayed posts that nobody here engaged with are purged after this many days (`18ee6657`). |
| `discovery_sync_per_host` (admin Discovery tab) | 0 (off) | How many of each host's top PeerTube channels and Castopod podcasts the instance actor follows and polls. Proactive sync does nothing until it is set (`cdc0ab1c`, `819fb1b4`). |
| `discovery_external_search` (admin Discovery tab) | on | Lets search include PeerTube videos from the wider network through SepiaSearch. Each viewer must still tick the option (`35b61d6c`). |
| `remote_image_cache_days` (admin Misc page) | 30 | **Now enforced.** Local copies of a remote post's images are deleted once the post arrived this many days ago, and pages load the image from its origin instead. 0 or empty keeps them forever. The column existed before but nothing read it, so an instance holding the old default of 30 starts evicting once `expire_old_media.sh` runs (see 2.2.1). (`f2182a54`) |
| `oauth_admin_role` (admin Misc page) | the role named Staff | Which role an app's `admin:read` and `admin:write` scopes stand for. A person holding it (or Admin) is granted them; anyone else is told on the consent page that they are left out. A token's admin reach is that role's permissions, as the person holds them. No admin API routes use them yet. (`52c495b8`) |

`config.py` no longer imports `app.constants`; it reads `VERSION` from
`app/constants.py` by path so that `import config` does not cycle through
`app/__init__.py` (`9f03cc02`). No action is needed.

Packaging: `pytest` was removed from `requirements.txt`. Test-only
dependencies (pytest, pytest-cov, pytest-timeout, pytest-xdist, respx, moto,
fakeredis, atheris) live in the new `requirements-test.txt`, installed only by
a new `test` stage in the `Dockerfile`, which sits above `runtime` so the
default build is still the production image (`c0f01c47`). atheris has no
aarch64 wheel, which is why it must stay out of the production install.

#### 2.2.1 Deployment

- **Schedule `expire_old_media.sh` hourly.** It expires relayed posts past
  `relay_retention_days`, enforces `remote_image_cache_days`, and removes
  PeerTube app uploads no post came of a day after their last part
  (`f2182a54`, W2.3). `docker.cron` runs it at half past each hour for a
  container with `CRON=true`; `INSTALL.md` and `INSTALL-docker.md` give the line
  for a host's crontab. An instance whose cron lines were copied from the guides
  before must add it. Relay expiry also still runs daily, but the media cap and
  the upload sweep run nowhere else. Once the job
  has run, the admin dashboard warns when it is more than two hours overdue; an
  instance that never runs it gets no warning, because the dashboard reads the
  jobs' last runs.
- **Video uploads gain 480p and 360p copies** (`23d94091`), made with ffmpeg on
  the `background` queue after the upload is ready. Expect that worker to use
  more CPU on an instance that takes video uploads.
- **The web container starts when data.lemmyverse.net does not answer**
  (`b0f9a296`). `entrypoint.sh` used to exit when `flask populate_community_search`
  failed; it now keeps the previous run's list and starts anyway.

- **A second Celery worker is required.** `entrypoint_celery.sh` now runs the
  queues named in `CELERY_QUEUES`, by default `celery,send` (the inbox and
  outbound delivery). Backfills, discovery sync and maintenance run on the
  `background` queue, so no long task can hold the slots the inbox needs
  (`0346d103`). `compose.yaml` adds a `celery_background` service with
  `CELERY_QUEUES=background` and `CELERY_CONCURRENCY=2`. **If you run your own
  worker containers or units, add one for `background`, or that queue is
  never processed.** The bare-metal `CELERYD_OPTS` in `INSTALL.md` already
  splits the queues this way. Each slot now prefetches one task, and a child
  process is replaced after 1,000 tasks (`05ec16c6`).
- **Proxy route for the Live view.** `/live/stream` must be proxied to the
  notification server (`piefednotifs:8000`) without buffering, like
  `/notifications/stream`. The `Caddyfile` does this, and `INSTALL.md` has the
  nginx `location = /live/stream` block (`4009cbd2`).
- **Caddy holds requests during a restart.** `reverse_proxy web:5000` now
  retries the dial for up to 30 s. A restart used to answer inbox deliveries
  with 502, and senders that do not retry lost them (`d7765780`).
- **`daily.sh`** also runs `flask refresh_discovery` and
  `flask sync_discovery` (`b693d2ef`, `57e51667`).
- **Images and versions.** Python 3.14, Redis 8.10 (from 6.2) and PostgreSQL
  pinned to 17.11 (`dd1d4231`). Python dependencies were updated to their
  latest compatible releases (`fa6ccbec`), among them celery 5.6, pyld 3,
  webauthn 3, stripe 16 and redis-py 6. `alembic>=1.12` is pinned
  (`3016d564`).
- **Build context.** `.env` files, every `pgdata` copy and `deploy/` are kept
  out of the Docker build context, so the secrets in `.env` no longer end up
  in the image (`0c8d8d37`, `0cf4e018`). Rebuild any image built from a
  checkout that held a `.env`.
- New CLI commands: `flask refresh_discovery`, `flask sync_discovery`,
  `flask discovery-seed-fixtures` (local validation data, no network) and the
  `flask relays` group, including `flask relays refresh`, which follows every
  accepted relay again after a restart (`cc41d630`, `e08a4e7f`).

### 2.3 Behaviour changes operators and users will notice

**State-changing links became POST forms.** Actions that changed state on a
GET (and so could be triggered by any page a signed-in user visited) are now
POST-only with a CSRF token. The buttons look the same; each is now a small
form, and the htmx variants post the same form.

- Joining a community, `/<actor>/subscribe` (`0e64d420`); leaving one,
  `/<actor>/unsubscribe`, and join-then-post, `/<actor>/join_then_add`
  (`d77ca3f7`). The feed and topic post pickers reach `join_then_add` with a
  307 so the browser re-posts its own form.
- Adding a community to a feed (`db7046ce`), joining a feed (`7e38e67d`),
  leaving a feed (`476c3745`).
- The notification bells: feed (`a595e044`), post and comment
  (`a14c2617`), profile (`f9c79194`).
- The feed dropdown's dead "None" item (a link to the removed
  `/feed/remove_community`) is gone.

**Invite and remote-interaction links land on the community page.** Because
joining is now a POST, the FEP-3b86 intent `/c/<actor>/subscribe` (the link
used in community invites) and `/activitypub/externalInteraction` no longer
join directly; they open the community page, where the reader confirms with
Join (`0e64d420`).

**Passwords.** Leading and trailing whitespace is stripped from a password
wherever it is set or checked: registration, login, settings, the reset link,
the admin edit, the API and NNTP. A password that was stored with edge
whitespace by a path that did not strip it will stop matching; the owner
accepted this, and affected users need a password reset (`6cbc0f7b`).
A password reset by any route (forgot-password, admin, CLI) now revokes the
user's API tokens, as the settings-page change already did (`09ded49e`).
`lemmy-import` now stamps `password_updated_at` too (`656fce82`).

**Passkeys.** Login now checks the authenticator's signature counter and
stores the real one; the migration resets stored counters to 0 so that no
existing passkey is refused (`0e4f61fc`). An unknown username gets passkey
options indistinguishable from a real account's (`db2bb045`), and an account
with no passkeys fails verification like any other failure (`70cde35b`).

**Login and accounts.** Web and API login share one finder that matches email
case-insensitively, so both accept the same forms (`4dac7396`). Disconnecting
an account's last login method is refused with a message to set a password
first (`9508049d`). A registration that fills the honeypot field is refused,
and the field is marked up so autofill skips it (`10500e84`).

**`resolve_object` requires authentication.** `GET /api/alpha/resolve_object`
used to let an anonymous caller make the instance fetch and store remote
content. It now answers the standard `incorrect_login` 400 without a token
(PERM-1, `c092b039`).

**Instance-banned users are refused in more places.** One helper now decides
whether a user is banned from a community, counting both community and
instance bans and a fresh ban row. Join-then-post, onboarding's topic join,
the feed and topic post pickers, the inbound Follow, profile-import joins, the
import tasks and the API follow all use it (D995, `ab8d9ade`).

**Moderation scope is one rule.** Every post, reply and community moderation
gate now asks one predicate, `can_moderate`: a moderator of the community, an
admin, staff, or a holder of the "administer all communities" permission
(`a8075d70`). Consequences:

- Staff may now edit a community and read its subscriber list.
- A site admin may restore and lock a reply.
- A *remote* instance's admin (an `InstanceRole` admin) no longer gets local
  moderation powers that some guards used to grant.
- Community delete and restore stay limited to the owner and admins
  (`c658f133`).
- An author undoing a moderator's removal of their own post gets 403, and an
  "administer all communities" holder may remove a post (`0dc5ecc2`,
  `758367a5`).
- Only an admin may ban an admin, and nobody may ban user 1, on web and API
  (`9dcaa2e9`). User 1's ban exemption is one named helper,
  `User.is_ban_exempt` (`8ab7ee7a`).

**User 1 receives admin notifications.** `Site.admins()` used to drop the
founding account when it had no role row, so it missed notifications sent to
admins. It is now included (`1b5f4d1a`).

**Private instances.** A private instance serves no sitemap, and a public
instance's sitemap omits posts from private and local-only communities
(R222, `8c269ad9`). RSS on a private instance works for a URL carrying a
member's valid RSS token and stays 404 for anonymous readers (R219,
`619de402`); page RSS links include the member's token (`c1f96cb0`). Earlier
in the campaign, every RSS feed (not just the front page), the embed, oEmbed
and calendar routes, five metadata routes and the modlog were brought behind
the private-instance check (`2cd72bf3`, `049f3a5b`, `6e51a243`, `afda6d4d`),
and the check now runs before the response cache and conditional-request
handling (`80b23c54`, `309e3569`).

**Deleted content.** A post its author deleted shows a "deleted by author"
placeholder for title and body, with its comments still visible; moderators
and admins see the original (D1085, `203f7c0d`).

**Follows.** A refused follow of a remote user is shown as "Request declined"
with a way to retry; a pending one is stored as NULL rather than `false`
(R265, `9d488574`).

**Uploads.** Every image upload on the post and event forms is capped at
10 MB (previously only `.gif` was) (R207, `82301ada`). `.mov` uploads and links
are treated as video like `.mp4` and `.webm` (D476, `2f42f431`). Genuine
`.heic` uploads are accepted (`831fb345`) and `.heif`-named files are accepted
and decoded the same way, including for community icons and banners
(`c8d26793`, `d3d09657`). The admin site-icon upload no longer accepts `.gif`
(D918, `61fbee82`). An SVG upload that cannot be sanitised is refused rather
than stored (`c0ac3305`).

**Search.** A logged-out search never returns NSFW content, whatever `nsfw=`
says, and the form no longer offers the choice (D798, `986c32ce`).

**Feeds.** The server refuses to rename a feed that has subscribers, including
crafted POSTs and the API (D696, `b9ff5cd5`). Admins may edit any public feed on
the web as the API already allowed (D697, `0186638a`); a private feed is its
owner's alone, admins included, on both (O5, `dc94f723c`). Creating an NSFW or NSFL
feed through the API is refused where the site disallows it (D708,
`31374f29`). `feed_auto_leave` defaults to off in the model as in the form
(D663, `4cf60b08`).

**Reply keyword filters work.** A user's reply filters were never applied.
A matching reply now renders as a collapsed "Filtered: <keyword>" stub with
its replies still visible. Admin and moderation views are unfiltered (R168,
`43512918`).

**Scheduled posts.** A scheduled post re-checks `can_create_post` at each
publication and skips (and logs) an occurrence that would be refused (F7,
`1cd3fea6`).

**Admin pages.** `/admin/` itself had no permission check and showed host
details to any signed-in account; it now requires admin permission
(`d665128a`). Every admin route sends an anonymous visitor to log in
(D943, `b7b58342`). Masquerading as an account writes an admin-only modlog
entry (D942, `e54d183e`). `/admin/perf_test` runs 1,000,000 iterations by
default with `?n=` clamped to that (R237, `4ed61765`). The LibreTranslate
status on the admin home is fetched with a 3-second timeout and cached for an
hour (D909, `8a83e0b3`). A new admin tool follows accounts from a Mastodon
server's public directory (`6659cdd4`).

**Request-path retries removed.** A failed remote actor fetch or feed
webfinger no longer sleeps and retries on the request or inbox thread; only
housekeeping Celery tasks retry (D738, D775, `2fe8a3be`, `cd562ced`). Profile
refresh tasks give up on a failed fetch instead of sleeping (D224,
`47091a3f`). The same rule now holds for every resolver and inbox fetch:
`get_request` retries only when a housekeeping task asks it to
(`7bb92a8a`, `b5cf4322`, `140c4cae`).

**Followers-only content.** Posts and replies now carry an ActivityPub
audience: `public`, or followers-only. Federated content records the audience
it arrived with (`dac11364`), and one predicate, `can_view`, decides access in
Python, the ORM and SQL (`f9beff5b`). A followers-only item is shown only to
its author's accepted followers, to the author it replies to, and to anyone it
mentions (`8fddcfb5`). Everyone else sees this:

- 404 on single-object views (`b04ba5e4`);
- nothing in listings, search, RSS and the sitemap (`a07b2e8e`), or on
  profiles, bookmarks and the user API (`f7ed8cd4`);
- a placeholder in a thread that keeps the reply's public children (`70e14d25`);
- notifications that name "a followers-only post" (`520dc82d`).

Moderators can still remove, restore, lock, sticky and purge such content
without seeing it, and their acknowledgements and the modlog do not reveal it
(`8a120e20`, `6805d599`, `9d0c7676`). Once every surface enforced this,
followers-only content from peers was accepted instead of refused
(`ecb72a2a`). Local users can now write followers-only posts in the local
microblogs community, and replies under them inherit the audience
(`7ab23572`, `40eb0c78`). Such a post cannot be moved, cross-posted,
quote-authorised or prefilled into a form (`d3d78eff`, `7a5e4b38`).

**Content warnings and galleries.** A peer's content warning is kept, and the
post or reply is shown collapsed under it everywhere it appears: teasers, the
post page, reply forms, polls, link previews, RSS and JSON-LD (`19d2d43b` and
follow-ups). Every image of a multi-image post is kept in a gallery
(`2e8ba47f`). Sensitive images, the gallery included, are blurred until
clicked, as are images under a spoiler flair (`9470a6fe`, `214a5888`). Gallery
images are hashed against the blocked-image list, and deleting a post deletes
its gallery's files (`2b8537f9`, `f39f2b6a`).

**Discovery of video channels and podcasts.** The daily `refresh_discovery`
reads opt-in directories into `discovery_entry`: SepiaSearch for PeerTube
channels, the Mastodon and Pixelfed directories, and index.castopod.org's
public export for Castopod podcasts (`f36343d0`, `0c264e79`, `9d994fde`,
`032a6bf9`). Community and people search fall back to these entries
(`3be4678d`, `f69acf06`). An admin can turn on proactive sync on the new
Discovery tab: the instance actor follows each host's top channels and
podcasts, polls them and unfollows the rest (`cdc0ab1c`, `36cf2c91`,
`819fb1b4`). A synced community that an admin deletes is never re-added
(`d996fc73`).

**Castopod podcasts.** A Castopod Podcast actor gets a community beside its
user, and its episodes land there instead of in microblogs (`fc1995e5`,
`9b1eb4ea`). An episode gets its audio player and cover (`6649d2a6`). Its
byline names the hosts and guests from the podcast's RSS feed, and links only
those whose account links back to the podcast (`b11f8251`, `4e211e38`).

**Media tab and video search.** A new Media tab on the home page shows
PeerTube and Castopod posts (`17be37b5`, `a35268b2`). Post search can be
narrowed to videos and podcasts (`e4c4c7d8`). A viewer can tick an option to
include PeerTube videos from the wider network (`35b61d6c`). Backfilled posts
are ranked by when they were published (`fd308889`).

**Live view.** A Live view now exists on these pages:

- the `microblogs` community, local or on another PieFed instance;
- microblog server views;
- the home page's Subscribed, Local, Popular, Media and All tabs.

New posts stream in over `/live/stream` and are held while the reader is
scrolled down (`96240c16`, `64089097`, `3bed3c37`, `65fc6e11`, `385aa20b`).
Live is off under a flair or tag filter. It falls back to polling when the
stream closes (`6f225b1b`).

**Microblog server views.** `/c/microblogs@<host>` shows the microblogs
community filtered to one server (`6e186e6c`). `/c/microblogs` lists its
busiest servers (`3bf5dae7`). Server views appear in `/communities` and in
search (`96304a8c`), and joining one brings that server's public microblogs
into Subscribed (`2e12b601`).

**Relays.** Admins can subscribe the instance actor to ActivityPub relays of
both the LitePub and Mastodon styles, on the federation admin page or with
`flask relays` (`6d1c89bb`, `cc41d630`). Relayed posts are marked. They are
kept only when they are top-level and wanted, and a relayed reply is kept only
in an open thread (`05ea2558`, `51c959da`). Relayed content is never
re-broadcast (`22850bf0`). A relayed post that nobody here engaged with
expires after the retention period (`18ee6657`).

**Quote posts.** An inbound post or comment that quotes another records the
quoted id, and its `RE:` fallback link is stripped (`a5651338`, `ae6cc321`).
The quoted object is shown as a card only to a viewer allowed to see it, and
as a plain link otherwise (`c2db4204`, `69198c28`). A card respects the quoted
object's content warning and the quoting item's NSFW blur (`5de16b58`,
`b97bd580`).

**Multi-language content.** A peer's `contentMap`, `nameMap` and `summaryMap` are kept
as language variants (`aad1aa31`). Each viewer sees the variant in their
language, with an "Also in" line to switch (`32b7fb95`, `3dcd2215`). Blocked
phrases and keyword filters check every variant (`3ea53a2b`, `afcac6c7`).
The translate buttons translate the variant shown (`c07b32e8`).

**Media objects.** A Create of a bare `Audio`, `Image`, `Document` or `Video`
object becomes a post (`cc3d0292`, `4fd45415`), and an Audio post plays on
its page even when its URL has no audio extension (`71fa95bf`).

**Input limits from the fuzzing track.** The fuzzing track (section 4) set
limits on input that could cost seconds of CPU or crash a request:

- **Long posts render truncated.** `markdown_to_html` renders at most 25,000
  characters of one text and puts `…` in place of the rest. The markdown2
  constructs whose cost is quadratic in their length (`![a::b](`, runs of
  `![` and of `<img `) are neutralised first; ordinary text is unchanged.
  The worst case measured was about 1 s (`cd0ffb7d`).
- **Images over 50 megapixels are refused.** This applies on every decode
  path: uploads, post edits, icons and banners, link thumbnails and the
  background resizing of federated images. The check runs at the header, and
  again for each GIF frame, ICO entry and TIFF tile, before a pixel is
  decoded. Pillow used to decode up to 179 MP, so a 6 KB PNG could cost seconds
  of CPU and hundreds of MB (`4522e3f5`).
- **Link thumbnails and federated images are capped at 16 megapixels,**
  counting every frame. This covers link thumbnails and the background
  resizing of images other servers send with posts, avatars, icons and
  banners. A 29-byte GIF could declare a 37.7 MP screen. Larger sources get
  no thumbnail or resized copies. Local uploads keep the 50 MP cap
  (`cc511621`, `a22ffc64`).
- **Stored images carry no EXIF or XMP.** With `MEDIA_IMAGE_FORMAT=AVIF`, the
  uploader's EXIF and XMP, including GPS position and camera make, were
  copied into the stored file. Every re-encoded image is now saved without
  them, and malformed EXIF no longer fails the save (`d3c06d7b`).
- **Uploads Pillow cannot handle are refused, not 500s.** The message is
  "filetype not allowed" on the web forms and a 400 on icon and banner
  uploads, and the file is removed. This covers images Pillow cannot decode
  (`bf8c428d`, `0ef14bf1`, `ad503683`), an icon in a format PieFed does not
  accept (`5ebe7b35`), and a GIF icon that cannot be written back
  (`360ba6ab`). The user-files, feed create and feed edit forms show the
  refusal as a message, also for an SVG that cannot be sanitised (`db648dd8`,
  `bff82774`). An icon or avatar stored before its banner is refused is
  deleted again, locally or from S3, on the feed create and edit forms
  (`0cb5f454`), feed copy (`f6778835`), community create (`b64bfb76`),
  community edit by its owner (`5eef3f16`) or an admin (`ed938962`), and the
  admin add-user form (`f247c97d`). With `MEDIA_IMAGE_FORMAT` set, an icon
  or banner upload no longer leaves its original beside the re-encoded file
  (`a6a9545c`). `.mpo` photos are stored as RGB instead of failing
  (`1f2aa073`).
- **A query string that is not UTF-8 gets a 400** on every route, not only
  the API (`5c4a5374`, `3270d449`).
- **Bulk follows are capped at 500.** The "Add people" box and the Mastodon
  follows CSV follow the first 500 handles, like the settings import, and
  the message counts those followed (`00d3a7e0`).
- **Settings import.** A user note in a PieFed settings export imports again
  (`49812275`). A file that is not a JSON object imports nothing, and an
  entry of the wrong shape is skipped (`25d15084`, `e372df4b`).
- **Flair colours must be `#hex`.** A peer's flair colour that is not one is
  stored as no colour (`061500ba`). The API (`18ef7e3b`) and the local flair
  form (`8b6e7857`) refuse it.
- **IPv6 bans match every spelling.** Domain bans, instance bans, user domain
  blocks, discovery's banned-domain check, the admin ban-list import,
  defederation subscriptions and `flask init-db` all compare and store IPv6
  hosts canonically. Migration 26 rewrites existing rows (`826e9783`,
  `1743d55f`, `3055db7f`, `bf837e71`, `4e1be989`).
- **`flask init-db` adds no empty banned domain.** A blank line in the
  No-QAnon or Peertube Isolation block list, such as the No-QAnon list's
  trailing newline, used to add a banned Domain named `''` (`2426de68`).
  A host on both lists, or repeated in one, is added once; 51 of Peertube
  Isolation's 52 hosts are also on No-QAnon, and unbanning one of the twin
  rows left the other banned (`e2dcc0e4`).
- **The admin ban-list upload** refuses a file that is not a ban list with a
  message (`72993d16`).
- **NNTP clients.** An article body keeps its indentation and trailing
  spaces (`8bbb0982`). A client that sends LF-only line ends works
  (`8e0aa522`). A line split over several reads is read whole
  (`a917b0f4`). LISTGROUP of a group not yet selected sends one status line
  (`a65b9389`). Malformed commands get an NNTP error reply instead of
  closing the connection (section 4).

**Moderation is visible to the author.** A local author gets a
notification when a moderator or admin removes or restores their post or comment,
turns its comments off or on, or changes the labels on their post (flair, NSFW,
NSFL, AI). It gives the content, the community, the reason and "a moderator" or "an
admin", and names no one. A label change is also a new modlog entry.

**Two moderation actions are permissions of their own.** "Remove and
restore content" and "Turn comments off" are split out of "moderate all communities",
so a custom role can hold just one of them. Holders keep both on upgrade. A role with
"moderate all communities" alone can no longer remove content or turn comments off.
A holder of the first can now also remove a comment on the web, which the API already
allowed.

**A community's text is its moderators'.** Its wiki pages, title,
description and rules can now be changed only by its own moderators. Instance roles,
Admin included, keep its other settings and may delete a wiki page. For an abandoned
community, an admin appoints a moderator first. The API refuses such a change with
`community_text_is_for_moderators`.

**Roles and permissions (RBAC).** An instance
admin now builds named roles in the admin area (`/admin/roles`, replacing
`/admin/permissions`). Each permission in a role reaches all domains or selected
domains; a grant says who holds the role, with an optional expiry. Admin is the
only built-in role. Changes take effect on the holder's next request.

**Staff changes on upgrade.** Staff is now an ordinary role an admin may edit,
rename or delete. It gains "administer all users", and all its domain-scopable
permissions are limited to the instance's first domain. Staff members can now
edit, add and delete that domain's accounts, and can no longer moderate or ban on
other domains, or act on remote communities and remote accounts. An admin who
wants the old reach sets those permissions to "All domains" on the role.

- Roles are created, edited, duplicated and deleted from `/admin/roles`. An
  instance-only permission (the editor marks them) always applies to the whole
  instance. A role with grants is deleted only once its grants are revoked,
  which the delete page offers.
- The user edit page's single Role drop-down is replaced by a Roles panel, and
  the add-user form takes a list of roles to grant. Only an admin sees either,
  and only an admin may grant roles.
- Every grant, revocation, expiry change and role change is in the modlog, for
  admins only, whatever the public modlog setting.
- A grant past its expiry stops working at once, and `flask send-queue` removes
  it with a modlog entry.
- The permission "shown as admin" makes its holder appear as an admin of a domain
  on that domain's host, in the API's site `admins` and `is_admin`, and on its
  About page. It gives no power by itself. A sister domain's About page now names
  only the admins homed on it, as its API already did.
- Profiles, the user preview and the About page name the roles an admin has
  marked "listed publicly" (Staff is), by their own names.
- A domain's registration setting is now "Anyone" or "Closed" and only governs
  public sign-up; "Staff only" and "Admins only" both became "Closed". Whoever
  holds "administer all users" on a domain may add accounts there, in any mode. A
  Closed site's accounts are no longer an admin's alone.
- The referral setting "Admins + Staff" reads "Admins and holders of 'generate
  referral codes'". Its stored value is unchanged.
- Only an admin may act on an admin: unbanning, deleting or editing an admin (the
  admin user form can ban) is refused to everyone else, as banning one was.
- A permission limited to some domains reaches those domains in the admin area:
  its lists of users, communities, content, reports, registration applications,
  referrals and domains, the report counts and the modlog show their rows, with
  a domain picker for anyone who may see more than one. The modlog can be filtered
  by domain.
- Notifications follow permissions. Registration applications go to holders of
  "approve registrations", held posts to "administer communities pages", reports
  on content to "moderate all communities" and reports on accounts to "administer
  all users", on all domains or the one concerned, as well as to the admins.

The work follows `docs/superpowers/specs/2026-11-03-rbac-design.md`; its Status
section records what is done.

**Third-party client apps.** Mastodon, Pixelfed, PeerTube and Lemmy apps, and
podcast apps that sync through gpodder.net, can now sign in to a PieFed account.
2.4 lists what each API serves; here is what operators and members will see.

- **Authorised apps** (`/user/settings/apps`, in the settings menu) lists every app
  a member has allowed, with what it may do and when it was last used, and takes
  any one's access back. An app's tokens never expire; revoking it, changing the
  password, a ban or deleting the account ends them (ruling M6, `bd9e7096`). A
  token works only on the domain it was issued on (`ec88a532`).
- **App passwords** for podcast sync are made on the same page: named, shown once,
  revoked like any app, and good for `/api/2` alone (ruling G1, `796fe990`). A
  podcast app's sign-in never takes the account password.
- **A private instance answers a few routes logged out**, the ones an app needs
  before anyone signs in: app registration and the token endpoint, OAuth's
  metadata, PeerTube's config and local client, Lemmy's site document and login,
  and Pixelfed's `/api/v2.1/config`. Every other new route checks its own token,
  and a token passes only for its own person.
- **Nodeinfo answers by agent.** Jerboa refuses a server whose nodeinfo does not
  say `lemmy`, so its agents (`Jerboa`, `LemmyKotlinApi`) are told Lemmy 0.19.11
  (ruling D3, `daf11055`). PixelDroid (`PixelDroid`) is told Pixelfed 0.15.2, with
  Pixelfed's configuration as `metadata.config` (ruling S6, `c0e007ea`), and
  Pixelfed's own path `/api/nodeinfo/2.0.json` says Pixelfed to anyone (ruling X1,
  `d141223a`). Every other caller, crawler or server is told PieFed, and the
  answers are cached and `Vary`-ed apart.
- **Chat requests.** A conversation someone starts with a local member who does
  not follow them is a request: it notifies nobody until the member accepts it or
  answers. The web chat lists requests apart, with Accept, and a member can mute or
  hide a conversation; a muted one notifies nobody who muted it (`e84dc5fb`,
  `85ad13ac`). Conversations from moderation, admin contact and the bot challenge
  are never requests.
- **Pictures in chat.** A message may carry up to four pictures. They federate as
  a post's attachments do and arrive the same way, and the web chat shows them
  (`a22c9518`, `5b87b982`, `bdd2a7ea`). The web form does not send them yet; apps
  do.
- **Local boosts and mutes** exist for apps (rulings of 2026-11-08). A boost is an
  Announce from the member's own actor (`f0e49765`); a mute hides an account from
  timelines, and optionally its notifications, for a while or for good
  (`32948b64`). Neither has a web button yet.
- **Push notifications** reach Mastodon apps through Web Push, on the clearnet
  only (`8aa72e3c`, `7937f335`). The VAPID key is made on first use and kept in the
  settings table.
- **Stories** in the Pixelfed apps are a member's microblog posts with a picture
  from the last 24 hours. Who saw one is recorded for that viewer alone; its
  author is shown no viewers (rulings X3, S1 to S5, `c9aadf19`, `40878b92`).
- **PeerTube app uploads** become video posts in video communities whose
  moderators may upload there, as on the web. "Internal" makes a local-only post
  (ruling U1, `88bac4d8`).
- **gpodder sync joins communities.** Adding a PieFed community's own feed in a
  podcast app joins that community, and removing it leaves; playing an episode
  that is a PieFed post marks it read (rulings G2 and G3, `35f5053a`, `03f0b400`).

### 2.4 API contract changes (for client developers)

- **`notif_type` 15, a moderation notice.** `GET /api/alpha/user/notifications`
  now returns it. `notif_subtype` is the modlog action, such as
  `delete_post`; `notif_body` is the notice; `reason`, `post_id` and sometimes
  `comment_id` come with it. `author` is the recipient, because the moderator is
  not named. A client that lists unknown types should show `notif_body`.
- **Internal errors are a generic 500.** Any exception that is not a
  deliberate refusal is logged in full and answered
  `{"code": 500, "message": "internal error"}` instead of echoing its text.
  Deliberate validation errors keep their message and status 400 (D895,
  `31e842fa`; earlier `d2d269d7` for `activity_result`, D187). Invalid client
  input such as an unparseable `page_cursor` stays a 400 (`f8353ce6`).
- **`resolve_object` needs a token** (see above, `c092b039`).
- **No session fallback for a bad token.** An image upload or delete with an
  invalid bearer token is refused instead of falling back to the browser
  session (D880, `68a0f066`). The alpha API keeps answering bad credentials
  with its 400 `incorrect_login`, not 401.
- **Logout** with a token that has no `jti` is a 400 instead of a false
  success (D1182, `da94895c`).
- **Comment views gain `filtered`**, mirroring the post API's flag: true when
  a "hide completely" reply filter matched (`43512918`).
- **Poll votes**: a vote is accepted only from a user who may reply in the
  poll's community (PERM-2, `530d06e8`). A single-choice vote for a
  non-integer or unknown choice is a 400 (D415, `9991ec20`).
- **Post and event URLs** submitted through the API must use `http` or
  `https` (`baf0e5cd`).
- **Distinguish**: a comment's distinguished flag is honoured only for users
  who pass `can_moderate`; otherwise it is ignored (D551, `80de7f08`).
- **Moderation**: an author restoring a moderator-removed post gets 403
  (`758367a5`); banning an admin requires admin, and user 1 cannot be banned
  (`9dcaa2e9`).
- **`image_alt_text`** is always applied to the post's image (D856,
  `63e81deb`).
- **Community edit**: the shared `edit_community` now sets every setting the
  web form sets, so an API edit can change them (D641, `5cbe9c0a`).
- **Feeds**: renaming a subscribed feed is refused (`b9ff5cd5`); NSFW/NSFL
  feed creation follows site policy (`31374f29`).
- **Leaving a community** you are not in is an idempotent success, not a 500
  (D598, `d1bcf6eb`). Repeating a post delete or restore is a no-op
  (`b021f31c`).
- **Upload size**: bodies over `MAX_CONTENT_LENGTH` get a JSON 413
  (`82301ada`).
- **Many "id nobody holds" 500s are now 400 or 404**, for example six view
  helpers (`50e941ff`) and nine query-string integer sites (`856361e2`).
- **Permission refusals** are logged at info level without a stack trace or
  Sentry event (D537, `76b12379`); this is server-side only.
- **Followers-only content** is "not found" to a caller who may not see it,
  including in `resolve_object` (`9620ffcc`, `5b82c286`). In a thread, a
  hidden reply is a stub with neutral creator, community, post and count
  objects, never nulls (`453b1d08`). The user API obeys the same predicate
  (`f7ed8cd4`).
- **New `extensions` fields** on posts and replies: `content_warning`
  (`4bfd238c`), the gallery's images (`2e8ba47f`; every picture of a gallery
  post, or the images beside a link) and, for podcast episodes,
  `podcast.credits`, listing only verified credits (`dcfa5b5b`).
- **Gallery posts.** On create and edit, `gallery` ([{url, alt_text}], two
  to `max_album_images`) is every picture and makes the post a gallery; `url`
  is not used beside it. On edit, leaving it out keeps the pictures; fewer
  than two is refused. A gallery post's `url` is its own page, as v1.8.10 gives
  it, and its `gallery` (v1.8.10's url list) and `extensions.gallery` list
  every picture.
- **Quotes.** Posts and comments show the id they quote, except when deleted
  (`3ee7755c`, `18c4b541`).
- **Language variants.** Posts and comments return their language variants
  (`0f652577`).

**New client APIs.** Each follows its own platform's shapes and errors, with
PieFed's visibility rules (blocks, bans, NSFW and NSFL settings, private
communities, local-only content, the domain's rules) and writes through
`app/shared/`, as the web and `/api/alpha` do. The specs give every mapping.

- **OAuth 2** (W0.2, `bd9e7096`): `POST /api/v1/apps`, `/oauth/authorize` (a
  CSRF-protected, frame-denied consent page), `/oauth/token` (authorization code
  with PKCE for public clients, refresh, client credentials), `/oauth/revoke` and
  `/.well-known/oauth-authorization-server`. Redirects are matched exactly; codes
  are single-use and last ten minutes; tokens are stored hashed. Mastodon's scopes
  are known; `admin:*` map to a role (2.2), Pixelfed's `security:*` grant nothing.
- **Mastodon `/api/v1` and `/api/v2`** (W1, spec
  `2026-11-07-mastodon-client-api-design.md`, rulings M2 to M8): accounts,
  instance, timelines (home is people followed; each joined community is a list),
  statuses and threads, notifications, search, media, favourites (upvotes),
  boosts, bookmarks, follows, blocks, mutes, follow requests, markers, filters and
  push. A titled post shows its title in bold; a status naming a community is a
  post there, otherwise a microblog post. A status id is a snowflake over posts
  and comments (`a2a3b65d`, `5c531097`). Not served: emoji reactions, admin
  routes, reblogs as entries in home.
- **Pixelfed** (W1.4, spec `2026-11-10-pixelfed-app-design.md`): `/api/v1.1`,
  `/api/v1.2`, `/api/pixelfed/v1` and `/api/v2.1/config` for its official app,
  PixelDroid and Fedilab: its account and status extras, direct messages on
  PieFed's chat (`/api/v1.1/direct/*`, one to one; group chats refused), and
  stories. Uploads named without an extension are known by their declared
  picture type (`d10266d2`).
- **PeerTube `/api/v1`** (W2, specs `2026-11-09-peertube-read-api-design.md`,
  `2026-11-09-peertube-login-design.md`, `2026-11-10-peertube-upload-design.md`):
  config, videos with `files[]` per resolution, channels (communities), accounts,
  comments, search, subscriptions (joins), ratings (votes), watch history (read
  marks), notifications (four kinds), the local client with the password grant
  (rulings P1 to P5), and resumable uploads (rulings U1 to U3). TubeLab is left out
  (P4).
- **Lemmy `/api/v3`** (W3, spec `2026-11-10-lemmy-v3-design.md`, rulings L1 to
  L4), over `/api/alpha`'s handlers, for any caller: login, site, feeds, posts,
  comments, votes, saves, read and hidden marks, follows, blocks, search, inbox,
  settings, reports and pict-rs-style image uploads. Moderation and admin
  endpoints answer `not_found` and the report count is zero (L2; W3b later).
  Polls, galleries and events show as the nearest Lemmy post.
- **gpodder.net `/api/2`** (W4, spec `2026-11-12-gpodder-sync-design.md`, rulings
  G1 to G4): sign-in by HTTP Basic with an app password, or the `sessionid` cookie
  `login.json` sets; devices; one subscription list per person shared by their
  devices; episode actions. Failed sign-ins are counted per address and account
  and refused with 429 past 20 an hour, before any password is tried.

**`/api/alpha` changes from the shared read layer** (W0.1). Moving alpha's reads
into `app/reads/` found five places where it told a reader more than the web does:

- A private community's comments were answered to anyone through
  `/post/replies` and `/comment/list` (`b997abfa`).
- A person's `moderates` named private communities to non-members (`e5b5a86f`),
  and a community's moderators were named when they are private (`9b069476`).
- The person list and search listed accounts that opted out of it (`d68b6871`).
- A person's post and comment counts included what the viewer could not list
  (`69ce7d90`).

Also: the unread private-message count counts messages, notified or not
(`abc4b9a2`), and CORS allows `PATCH` for a browser app's profile update
(`bd140a63`).

### 2.5 Federation and wire-format changes (for other implementers)

**Responses this instance serves**

- **410 Tombstones.** A deleted local post is served as 410 with an
  ActivityPub `Tombstone`; private, local-only and unpublished posts are 403.
  (The 401 when the author blocks the requesting instance was dropped after
  the rebase, as v1.8.10 dropped it.) Visibility is checked
  first, so a Tombstone never confirms the existence of something the caller
  may not see. `post_ap`, `post_replies_ap` and the post context share this
  gate (D189, D199, D190, `dfcfea20`, `500378d2`). A remote reply's URL
  redirects to its origin and a deleted local reply is a Tombstone (D191,
  `d8649293`).
- **Cache-Control policy.** Every ActivityPub endpoint takes its
  `Cache-Control` from one table: actor profiles 300 s, content objects
  120 s, collections 60 s; 404 and 410 responses are `no-store`
  (D160, D180, D195, `2a0c102a`; D196, `c61e282f`). Feed profiles now send
  `Vary: Accept` (D161, `bc7de449`).
- **Outbox paging.** User, community and feed outboxes keep `totalItems`
  (now a real count, `c51b6ef0`) and their first page inline, and add `first`
  and `last`. `?page=N` serves an `OrderedCollectionPage` of 50 items with
  `partOf`, `next` and `prev`; any value other than a positive integer is a
  400 (D173, `ec61f68e`).
- **Collections.** Feed collections list moderators by `public_url()`, use
  the lowercase id, and the feed outbox is an `OrderedCollection`
  (D183-D185, `4fd38284`). The moderators collection lists real moderator
  rows only, with no synthesised owner (D174, `edb48a79`). A community's
  featured collection lists only published posts (D179, `72310e40`).
  A followers collection hides followers the user has blocked (D182,
  `5da948fd`). A browser asking for an ActivityPub collection is redirected to
  the HTML page (D177, `04bab86a`).
- **Webfinger is stricter.** A resource for another domain is 404 instead of
  serving the same-named local actor (D145, `f19b7f85`); a resource is parsed
  as `acct:` only when it starts with `acct:` (D146, `c90b8e9f`); malformed
  resources are 400 (`a1a5cc48`); private, banned and deleted feeds and
  communities are no longer advertised (`f466144d`, `b34eba8d`); a user's
  private-voting `alt_user_name` no longer resolves (D166, `1997fa7f`); feed
  names match case-insensitively (D148, `6ce23f2a`).
- An ActivityPub request for a remote user's profile, `/u/<user@host>`, is a
  400, as it already was for remote communities (D156, `b804d904`). An HTML
  request by a signed-in user resolves the remote handle instead
  (`0254fdec`).
- An ActivityPub GET no longer creates an `Instance` row for its caller
  (D193, `8c90ca20`).
- **Gallery posts** (type 7) carry every picture, in order, as a `Document`
  (a WebP, AVIF or HEIC as its JPEG rendition); the `Page` form adds a `Link`
  to the post's page, the `Note` form does not. `image` is the thumbnail. An
  incoming post with two or more image attachments is stored as a gallery.
- `/quote_boost_auth` vouches only for quotes this instance recorded accepting
  and answers 404 for anything else (R205, `35abcf19`).
- **JSON-LD content type.** A peer asking for `application/ld+json` gets
  documents served under that type (`91f13054`).
- **Followers-only objects** are served only to a signed fetch by an entitled
  requester, or by anyone they were pushed to. Answers that depend on the
  signer send `Vary: Signature` (`140a090f`, `9cc4b7bc`, `f1506ff8`).
- **Secure mode** (`require_signed_fetch`, see 2.2) refuses unsigned
  ActivityPub GETs. Actor documents fall back to a minimal form (`89cad496`,
  `7705d2f1`).
- **New and completed collections.**
  - A Person names count-only `followers`, `following` and `liked`
    collections (`2d3e2beb`, `acc5900a`).
  - A Group names a count-only `following` collection (`b92b1191`).
  - A user's outbox lists their public posts and comments, and is refused to
    an instance the user blocked (`2d856085`, `eeacf388`).
  - Local posts embed count-only `likes`, `dislikes` and `shares`
    collections, and local comments `likes` and `dislikes`. These are also
    served at their ids (`7775f2bd`, `4760d6ea`,
    `498c295f`).
  - `liked` counts only votes whose last federated activity was a Like
    (`097ff04f`).
- **Private communities.** A private or local-only community's outbox, and
  its featured, moderators and followers collections, are refused
  (`7469b48a`, `63bb5879`).
- **Tombstones.** A deleted local user answers 410 with a Tombstone
  (`96571bc3`). A deleted post's Tombstone carries the type it was federated
  as (`b403c3a0`).
- **New endpoints.** `/private_message/<id>` is served to the parties to a
  DM and is 404 to everyone else (`485b12db`). `/actor/inbox` and
  `/actor/outbox`, which the instance actor advertises, now exist
  (`cf83adf6`, `5930ed07`). `/activities` serves a logged activity only to
  someone its addressing entitles (`67ebc10a`).
- **Discovery links.** Post, profile, community and feed pages advertise
  their ActivityPub id as `rel="alternate"`, in a `<link>` tag and a `Link`
  header, but only when the id is a clean ASCII URL (`c3b48d36`, `19ea9a87`, `e4bd70dc`).
- The instance actor has a name, so Castopod can store it and accept its
  Follow (`e44ff591`).

**Activities this instance sends**

- **Flag shape for chat reports.** Reporting a conversation with "report
  remote" ticked sends the reported member's instance a `Flag` whose object
  lists the reported account's actor first, then the message ids, as Mastodon
  and Lemmy expect (D761, `9390ec0a`, `dbc61619`).
- **Events.** A federated Event omits optional properties it does not have
  instead of sending `null` (D306, `36447ab8`). An event post's URL is
  attached as a `Link` on both send paths (D300, `8f5cc333`), and the new
  "More info" URL is an extra attachment `{type: Link, href, name: "More
  info"}`, read back on ingest (R223, `d8f7a9f5`). A null timezone is
  localised to UTC (D307, `b81a5347`).
- **Announce batches capped at 100.** Our batched Announces carry at most 100
  objects (D54, `63de70d3`). The limit is `ANNOUNCE_MAX_OBJECTS`.
- **Accept re-send.** A re-sent Follow for an already-accepted relationship
  gets the Accept again; one for a pending relationship is logged and ignored
  (D66, `8117d043`).
- An Undo of a Follow carries the original join request's id for every peer,
  not only ovo.st (D89, `2db1e1f4`).
- **Retry policy.** An outbound activity whose connection fails (refused,
  DNS, timeout) is queued for retry on the same exponential backoff as a 429
  or 5xx (D767, `72f23dd6`). Inbox processing never retries in-line (D775).
- **Outbound DNS pinning.** All outbound HTTP through the shared httpx client
  (and the Stripe client) resolves a host once, refuses it if any address
  fails the SSRF guard, and connects only to a checked address. The URL is
  unchanged, so the `Host` header and TLS SNI still carry the hostname, and
  each redirect to a new origin is checked again (R162, `ea802274`). The
  LibreTranslate client is deliberately left unpinned because its endpoint is
  the admin's own, often on a private network.
- Private and local-only communities no longer federate deletes, bans, edits,
  votes, locks, reports, moves, chosen answers or replies to other instances
  (for example `3fc0c331`, `c9929705`, `b80564c6`, `315313d7`, `7db42dca`).
- **RFC 9421 double-knock.** Outbound deliveries and signed GETs are signed
  in the format the peer is known to accept, cavage by default. A 401 gets
  exactly one retry in the other format (RFC 9421 HTTP message signatures),
  and the format that succeeded is recorded in `instance.signature_format`
  (`c0219bec`, `e64c7afc`).
- **Followers-only posts** written here federate to the author's followers
  (and anyone mentioned) only, never through the community. The same goes for
  their replies and Deletes (`0361f082`, `40eb0c78`, `c3692897`). Activities
  about a followers-only object (votes, moderation, poll votes) are not
  relayed to a community's followers (`02d768da`, `0b9c0622`, `c2daf129`).
- **Relay subscriptions.** The instance actor sends Follows to relays. A
  Mastodon-style relay Follow is addressed to nobody, as Activity-Relay
  requires (`6d1c89bb`, `fc40cac3`).
- **Discovery sync.** The instance actor follows and unfollows synced PeerTube
  channels and Castopod podcasts. A Follow that a peer refuses outright is
  shown as "Could not deliver" and is retried after a week (`34e755db`,
  `3ec16fd5`).
- **Moderator edits of a local community** are relayed as the community's own
  Update, carrying only what was applied (`8c9f5dfa`, `0f91cd1c`).
- **Capped responses.** A federation delivery reads at most a capped response
  body, and a delivery that cannot be sent is not requeued (`c9e1b9c6`,
  `acf8e6f7`).

**Activities this instance accepts**

- Followers-only and direct content is refused at ingest rather than stored
  as if public (`65c524b1`). Direct messages are unaffected.
- An inbound Announce of a list refuses nested lists and processes at most
  100 objects (D54, `63de70d3`).
- Content fetched outside the inbox (resolve, search, outbox backfill) must
  pass the same author gates as an inbound Create; a refused item is skipped
  and logged (PERM-3, PERM-4, PERM-5: `14d1aa32`, `aad727a4`, `f9636a51`,
  `655c22ec`). Backfill requires the moderators collection to be on the
  community's own host (`e698f32d`) and walks a paged outbox from `first`
  along `next` until 50 items or 10 pages (`055e0937`).
- An inbound edit to an existing post is refused when the editor may not
  post in the community, as reply edits already were (D141, `cf9ff80a`).
  Federated reply Updates follow the post rules (D249-D253, `3a556482`).
- A reply Create carrying `repliesEnabled` stores it (D269, `a7258221`).
- A second vote in a single-choice poll replaces the earlier one (D120,
  `d8ac9d2a`).
- Boosts of microblog posts by followed accounts are ingested and surface in
  the feed, and an Undo of such a boost is handled (`f1d8cb26`, `a1f5eb35`,
  `b8bcd7ce`).
- Many malformed inputs that used to raise are now refused and logged: an
  Announce with no id, type or actor (`4ede2bc1`, `07ebcdd7`), an
  Accept/Reject of an untyped object (`4e12249d`), a `name: null` post
  (treated as untitled, D258, `69fe67ae`), an empty `contentMap`
  (`ec09c177`, `3bbd157d`), a nameless Hashtag (D246, `409b03f2`).

Since the register closed:

- **Signatures.** RFC 9421 signatures are accepted inbound (`466beb8a`). For
  LD signatures:
  - every signature option is hashed, as Mastodon does (`c9d97c6d`);
  - an expired signature is refused (`3eaa085f`);
  - an LD signature is honoured only for documents whose JSON it actually
    covers (`239c756b`);
  - an LD-accepted activity takes its content from the origin (`8fb40d77`);
  - the standard JSON-LD contexts are served locally, and a context named by
    an incoming document is never fetched (`e8b9fc38`, `6e4db969`).

  A signature whose keyId names another host is refused (`c7a8ea9f`). A
  signer's key is refetched once when a new key could cure the failure
  (`e54fa275`, `3b267a61`). Signatures that leave required headers unsigned
  are logged, or refused under `SIG_REQUIRED_HEADERS_ENFORCE` (`d3e67c06`).
- **Dedupe.** An activity id is remembered only after its signature verifies
  and its hand-off succeeds, and dedupe keys are namespaced by actor host
  (`9f904476`, `de44a796`, `e98705d2`).
- **Announced activities.**
  - An Announced Create or Update is always refetched and must match the id
    and author it names (`0b153432`).
  - An Announced activity from another host must name an actor on that host
    (`540e7a91`).
  - A cross-origin Announced Delete needs a community moderator and waits
    `cross_origin_delete_delay` unless the origin confirms the deletion
    (`59a6cbec`, `38b61625`).
- **Trust of hosts and inboxes.**
  - A remote actor's or instance's inbox is trusted only on its own host
    (`616949e1`, `c0b51cda`).
  - An actor found by WebFinger is stored under the handle its own server
    confirms (`72287130`, `5dcbeb53`).
  - A lookalike host is no longer taken for this instance (`027398b3`).
- **New activity handling.**
  - A remote actor's Update of itself refreshes its profile, key and inbox
    (`699f1c2f`).
  - An Undo of an EmojiReact takes the reaction back (`adb84a5d`).
  - An Undo of a Flag withdraws the report while moderators have not acted on
    it (`47f461b2`).
  - An Undo of an Add or Remove reverses the sticky, moderator or feed change
    (`459cb5fd`).
  - A Group that deletes itself becomes read-only (`f81c43ef`).
  - A federated Remove may not leave a local community with no moderators
    (`7b20d125`, `7edbaae8`).
- **New object types.** An activity with an array-valued `type` is dispatched
  (`edcb3580`). A Castopod Podcast actor is accepted as an author
  (`4c55f937`). An Application actor is a bot user unless it stands for a
  whole server (`7b67e5d7`). A Create of `Audio`, `Image`, `Document` or
  `Video` becomes a post (`cc3d0292`). `quote` ids and `contentMap` language
  variants are stored (`3b824624`, `aad1aa31`).
- **Relayed content.** A relay's Announces are stored as relayed posts.
  When a forward from an accepted relay has no verifiable LD signature, the
  object is fetched from its origin instead of being refused (`027478a3`,
  `3700c887`).
- **Fixes.** `process_delete_request` is a Celery task again, so a remote
  account deletion no longer crashes `/inbox` (`c23b1261`). Create and
  Announce ids up to 255 characters are kept (`49f3bf17`). A peer's published
  date is clamped to now (`5ddb3ddd`).

**From the client-app APIs**

- **Local boosts.** A member's boost is an `Announce` from their own actor to
  Public, cc their followers and the post's author, delivered to every instance
  holding an accepted follower of theirs and to a remote author's inbox; an
  `Undo` naming the same id takes it back, and goes whatever the post later became
  (`f0e49765`, `832ba895`). Only an open post outside a private or local-only
  community may be boosted, and not by someone its community banned or its author
  blocked.
- **Pictures in chat messages.** A message's `Create` and its object carry its
  pictures as `attachment`, one `Document` each; WebP, AVIF and HEIC go out as
  JPEG (`a22c9518`). An incoming message keeps up to four `Image`s, or
  `Document`s of an image type, over https, as links to the picture, fetching
  nothing (`5b87b982`); a peer cannot have an address of ours shown as its picture
  (`f16d7ae9`).
- **Video renditions.** A published `Video`'s `url` lists the 480p and 360p
  copies once they are made, and is updated then (`23d94091`).
- **Internal videos stay home.** A PeerTube app upload marked Internal is
  local-only: the outbound gate refuses any activity naming it (ruling U1).
- **Nodeinfo by agent.** Only Jerboa's and PixelDroid's agents, and Pixelfed's
  own path, are told another software's name (2.3). Servers and crawlers are told
  PieFed, as before.

---

### Payments

- Stripe keeps working from the environment until imported, and existing plans and donors
  carry over. Add `PAYMENT_SECRETS_KEY`, then use Import on Admin > Payments.
- Add `invoice.paid`, `charge.refunded` and `checkout.session.async_payment_succeeded` to
  the Stripe webhook, or renewals and refunds won't appear in the ledger.
- `/plan_unsubscribe` and `/stripe_redirect/<plan>` are gone, replaced by POST routes under
  `/donations`. `/stripe_webhook` still answers.
- Deleting an account now cancels its subscriptions at the provider.

## 3. Security fixes

These are the fixes with a security impact, grouped by kind. Twelve carry the
`security:` prefix; the rest were found by the campaign and committed as
`fix:`.

**SSRF and DNS rebinding**
- DNS rebinding past the SSRF guard closed by connection pinning (R162,
  `ea802274`); the NNTP server's image fetch now goes through it
  (`024d0736`).
- The SSRF guard allowed every IPv6 literal, including `[::1]` (`ee3491b8`).
- Anonymous callers could drive outbound fetches through `resolve_object`
  (PERM-1, `c092b039`) and an unauthenticated outbound request in the post
  fragments (`135e5d37`).

**XSS and unsafe markup**
- A remote community's posting warning was rendered unescaped (`|safe`) on
  every post page (`cef41575`).
- A post's teaser paragraph decoded entities and re-rendered them as live
  markup in the feed (`d6a2b164`).
- URL scheme checks: `is_image_url` accepted `javascript:` (`e3136380`);
  three more URL predicates (`a931b8aa`); a peer's post URL (`493fa8f3`) and
  Event links (`01d9c1c3`); API post and event URLs (`baf0e5cd`);
  `File.source_url` (`eca953ae`); object ids on every path (`ad8126e7`).
- SVG uploads that cannot be sanitised are refused (`c0ac3305`).

**Open redirects**
- One origin check for every user-influenced redirect target, replacing a
  substring test (`0c923ae2`, `c0f01c47`), with the admin-configurable policy
  (`5f531bf7`).
- Open redirects on the remote-follow form (`f853cd62`), the share button
  (`d6105784`) and the anti-bot interstitial (`90857103`).

**CSRF on GET**
- Join, leave, join-then-post, feed add/join/leave and all notification bells
  converted to POST with CSRF (see 2.3). A ratchet test now fails on any new
  mutating GET (`e2de0f5c`, extended in `7e38e67d`).
- A GET cleared a post's flair (`da73ba92`); loading an image could make an
  admin sticky a post instance-wide (`7882c145`).

**Authorisation gaps**
- The permission call-site audit's five deferred items, resolved: anonymous
  `resolve_object` (PERM-1, `c092b039`), poll voting with no permission check
  (PERM-2, `530d06e8`), fetched content stored without ban or allowlist gates
  (PERM-3/4, `14d1aa32`, `aad727a4`, `f9636a51`), outbox backfill attributing
  posts to any instance (PERM-5, `e698f32d`, `655c22ec`). Earlier audit
  findings F5, F7, F10, F12 and F13 were fixed (`cf9ff80a`, `1cd3fea6`,
  `4fd3a07e`, `5ec4e845`, `d665128a`); `choose_answer` / `unchoose_answer`
  gained a check (`e4518954`).
- Anyone could silence anyone's post, and a post could be moved into a
  private community (`ff55d3b1`).
- A moderator of any community could undo another community's moderation
  (`f27f6ec1`), or repoint or delete another community's RSS importer
  (`9b9cbf89`).
- An unban anyone could trigger (`c44c0b3b`); a community ban that did not
  stop joining (`3e7a3d29`); a banned account could file reports
  (`00bdbe1e`).
- Any account could put any private conversation in front of the admins
  (`083b71a9`).
- The OAuth signup path bypassed every user-name rule (`d73859e7`).
- `invite_with_chat` and `invite_with_email` now check `can_invite`
  themselves (D632, `8e552fcd`).
- Five form `validate()` overrides discarded `super()`'s verdict
  (`0236e3fc`).
- A username with regex metacharacters could claim feed URLs in another
  user's namespace (`6dcec45b`).

**File and storage safety**
- A peer could delete any file on this server by naming it in a post's image
  URL (`2066742c`), or any object in the S3 bucket (`e85bfd43`).
- Imported ban lists are no longer written into the served media directory
  (D930, `00987ce7`).

**Private community and private instance leaks**
- A private community's page refused to non-members (`b64fd1a2`); private
  communities no longer offered as a post destination (`862c1520`); the
  private-community filter applied for every feed viewer (`2b06afeb`).
- Leaks through the author's RSS feed (`fcfc2823`), the cross-post form
  (`3a79dfd0`), the front page (`5d7d5303`), a profile tab (`1fa1e33b`), the
  embed pages (`da73ba92`), the ical, tag and topic routes (`135e5d37`,
  `17a6dbdb`, `d3583b9e`) and several more (`d6105784`).
- Private feeds hidden from every route that takes their id (`5a6c5289`,
  `ee09d6c8`).
- ActivityPub post and reply endpoints enumerated replies of private,
  local-only, unpublished and deleted posts (`dfcfea20`).
- The private-instance gate extended as listed in 2.3.

**Information disclosure**
- Every uploaded file's URL was enumerable by id (`8e28770c`).
- Internal exception text is no longer returned by the API (`31e842fa`,
  `d2d269d7`) or leaked from a peer's refusal (`915f584e`).
- Passkey endpoints no longer reveal whether a username exists or has
  passkeys (`db2bb045`, `70cde35b`).
- The feed cache key is scoped to the user, not a client-supplied result id
  (`cb35ea12`).

**Token and session revocation**
- A password reset by any route revokes API tokens (`09ded49e`).
- An RSS token kept working after the account was banned or deleted
  (`731b535f`).
- An invalid bearer token no longer falls back to the session (`68a0f066`).

**Client IP and rate limits**
- The client IP was read from client-controlled headers (`6acda199`); the
  rate-limit exemption was a substring test (`0228d697`).
- The registration honeypot was a bypass (`620c8e5e`, `10500e84`).

**Signatures and replay**
- The admin replay refuses an activity that originally failed signature
  verification (D45, `9ad310bb`); a value-truncating Signature header parser
  was fixed (`5e4df446`) and the two parsers merged (D768, `69ced92a`).

### 3.1 Since the register closed

The ActivityPub security track and the fuzzing track found the following. All
were committed as `fix:`.

**Forged or replayed federation**
- **LD signatures.**
  - An expired signature was accepted, and its expiry was not read from what
    was signed (`3eaa085f`, `7e411523`).
  - A signature was honoured for JSON it did not cover (`239c756b`).
  - An LD-signed actor Update could replace a key without a fetch
    (`c7152a45`).
- **Announces.** A cross-origin Announce could inject a Create, Update or
  Delete for another server's content (`06e88459`, `0b153432`, `59a6cbec`).
- **Signer identity.**
  - A signature made for another instance verified here (`67765836`).
  - A keyId could name a different host from its actor (`c7a8ea9f`).
  - An unsigned ChatMessage could claim the fediseer actor (`0f3c49d8`).
- **Stored inboxes.** A remote actor or instance could point its stored inbox
  at another host (`616949e1`, `c0b51cda`).
- **Local communities.** A moderator's Update of a local community could
  replace its key or ids (`361b7b10`).

**SSRF and resource exhaustion**
- A JSON-LD context named by an incoming document was fetched (`6e4db969`).
- Outbound GETs and POST responses had no size or time cap. Every fetch is now
  capped at the transport, with compressed bodies counted and decoded within
  the cap (`f03bb7be` to `cf48b4b2`).
- LD verification normalised arbitrarily nested JSON-LD, whose cost is cubic
  in the nesting. Nesting past 32 levels is now refused (`45d4887a`).
- Fifteen markdown and HTML sanitiser regexes backtracked catastrophically on
  unclosed input, a ReDoS. Each was fixed to run in linear time (`ac6849fa` to
  `26924027`).

- Images up to 179 MP, a few KB on the wire, were decoded at full size.
  Images are now capped at 50 MP, and remote link thumbnails and federated
  images at 16 MP, counting every frame (`4522e3f5`, `cc511621`,
  `a22ffc64`).
- markdown2 constructs with quadratic cost could hold a request for minutes;
  they are neutralised and rendering is capped at 25,000 characters
  (`cd0ffb7d`).
- The NNTP server buffered a line without limit before refusing it, and its
  range commands looked up every number in a client-supplied range
  (`535eefee`, `6587c304`). An API JSON body nested deeply enough to exhaust
  the decoder was a 500 (`f42e2dc0`).

**Ban bypasses**
- `url_is_storable` read a host differently from a browser, so a banned
  domain could pass:
  - written with an ideographic full stop (U+3002) (`c6a023f6`);
  - as a shorthand IPv4 address such as `127.1` (`8732cc39`);
  - with a space or another character no hostname holds (`b7532ce0`,
    `e3d817fa`).
- An IPv6 address in a spelling other than the one banned passed domain,
  instance and discovery bans (`826e9783`, `1743d55f`, `3055db7f`).
- `is_refused_address` allowed IPv6 site-local addresses, `fec0::/10`
  (`115b4a58`).
- Discovery's host check accepted a last label that a browser reads as an
  IPv4 address, all digits or hex (`b42bd82c`, `a826c497`), and a URL
  holding a backslash, which browsers read as `/` (`c0e0f404`,
  `253d9abb`).

**Information disclosure**
- Stored AVIF images kept the uploader's EXIF and XMP, including GPS
  position (`d3c06d7b`).
- `/activities` served any logged activity to anyone (`67ebc10a`).
- Followers-only content leaked through moderator acknowledgements, the
  modlog, notifications, cross-posts and the purge page (`6805d599`,
  `caad8d68`, `c277b0dc`, `7f1f8c3a`).
- `.env` files, with their secrets, were copied into the Docker image
  (`0c8d8d37`).

**Other**
- A crafted Signature header 500'd a public ActivityPub GET (`5257b848`).
- `is_local()` took a lookalike host for this instance (`027398b3`).
- A peer URL whose browser-visible host differed from the host the ban check
  read could pass a domain ban (`ea159eaf`, `166d491b`).
- A comment's translation was swapped into the page unsanitised (`4ff01d58`).
- The actor-link filters linked handles inside links and code spans
  (`f9065067`), and rewrote handles inside tag attributes such as `alt`
  (`213bead8`).
- A peer's title or name holding a CR or LF, or a body line of whitespace and
  a dot, could end an NNTP reply early and inject lines into it (`5a8c50f5`,
  `412f7f63`).
- `links_with_parens` turned an HTML comment after a link into visible text
  (`bad2a3d3`).

### 3.2 In the client-app API tracks

Each phase had a security review before it merged, and an automated reviewer read
every commit. What they found, in code that never shipped without the fix unless
noted:

**OAuth and sign-in**
- On a private instance the login gate let any OAuth token through to all of
  `/api/`, where `/api/alpha`, which does not read OAuth tokens, served the caller
  as nobody: a token with no read scope could read the instance. The gate no longer
  reads OAuth tokens; each `require_oauth` route checks its own (`517ddfd0`).
- A stolen refresh token worked without the app's secret; a refresh now needs
  it, and two refreshes at once yield one pair (`e8ec0c93`). An authorization
  error is sent only to a registered redirect
  (`fa6ff997`); a registered redirect may not hold whitespace (`5dbb6c75`).
- PeerTube's password grant counts its guesses in one bucket, whatever endpoint or
  form the request takes (`dc199b6d`, `5371836f`).
- gpodder: failed sign-ins are limited before a password is tried (`736918d3`),
  counted per address and account so that cookieless onion clients, who share one
  address key, cannot lock each other out (`36b00122`); a wrong password is refused
  even beside a good session cookie (`e9cefd7b`).

**What a reader is told**
- A Mastodon status id kept 15 bits of the row id, which a remote server's chosen
  `published` time could collide; it now holds the whole id (`5c531097`).
- A paging bound naming a status the viewer may not read revealed that it exists
  and when it was posted (`c94f0dcf`).
- Status and media writes echoed exception text in their errors (`be4539d5`,
  `bfa18fd4`).
- A push told an app more than the notifications list would, or for another domain
  (`a0c98295`). A push resolved its endpoint's name again to connect, so a name
  that answered differently after the check (DNS rebinding) could reach a private
  address; pushes now go through the pinned client, under a deadline
  (`c686ceb8`).
- PeerTube's `users/me` gave the email to any token; now only to the login the
  password was given to (`06174e55`).

**Writes and limits**
- A member banned from a community, or blocked by a post's author, could boost it;
  a boost's Undo was gated like the boost, so it never left once the post changed
  (`832ba895`).
- A person banned from a community could rejoin it through PeerTube's subscribe:
  only the alpha route checked the ban. The shared join now refuses a banned person
  and a deleted Group for every caller, before any row is written (`7376cef2`).
- Choosing Public in the PeerTube app could unstamp a local-only domain's video
  (`7625c6a2`), and uploads no post came of escaped the upload limits (`ed35c3b7`).
- Lemmy's hide-posts took any number of ids, of any type (`0515117f`).
- gpodder: removed subscriptions, kept so that `since` can report them, were not
  counted against the cap (`c33c60d1`); episode actions are capped from the start.
- A chat peer could have an address of ours shown as its picture, a request with
  the viewer's cookies (`f16d7ae9`).
- A story's self-expire took its id under a name the hidden-row gate did not check
  (`979c7107`).

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
`db7e1dff`; host differing only in case or port, D21/D24, `1f768fef`,
`8eddc7af`), counters left wrong (a Reject or unfollow lowering follow counts
for a follow that was never accepted, D75/D557, `7bb1189a`, `f02e62ce`),
rejecting a follow that accepted it (`8e28770c`), and federated posts that
were never created at all (any post mentioning a local user, `2b3ad317`; every
post when there was no `Site` row, `e41a1a66`). A flagged domain silently
discarded posts; moderators are now notified instead (`5baf490a`), local
moderators only (D288). Logging was corrected so that each outcome is
recorded under its real type (D63, D68, `c07f0ac1`, `f56adbdb`).

**Outbound federation.** Sends that crashed on missing data now federate (a
post whose Poll or Event row is missing, D490, `fb949b43`; a send with no
body, D768, `fc7f8018`); a delete's follower fan-out skips dormant instances
(D335, `5eb1fa26`); sticky no longer federates a change it refused to make
(`8f4d78a3`); a delete that never left the instance (`50152622`); the
private-community send gates listed in 2.5.

**Content ingest and display.** A post could be missing from every page of a
sort (`43a98a7f`); an unpublished post was public (`2505e58b`); an Update
whose URL cannot be stored keeps the existing one (R213, `2d691b70`); a peer
Event was lost unless it carried all fourteen optional fields (`05e2db73`);
`rewrite_href` now rewrites a URL that matches a local post (`c69fb4d4`);
attachment pre-checks scan every attachment, not only the first (R202,
`1915c642`); a stored post URL that 500'd every listing (`a5fa2527`).

**Moderation and reports.** One moderation predicate (`a8075d70`); reports
name the right source instance (D440, D550, D553, `03a5a71c`, `60bbd960`);
the minor-abuse escalation actually fires (`921bf4fb`); a retransmitted
instance Block is a no-op (D205, D94, `497a29f3`); bans decrement cross-post
reply counts (D206, `aae9e9ef`); an admin or staff member with several roles
can no longer be blocked (D558, `f4fd81da`); local communities gain an NSFL
control (R203, `327fcddd`).

**Communities.** Leaving as a non-member is idempotent (`d1bcf6eb`); the web
edit form goes through the shared `edit_community` (D641, `251eaedf`);
community refresh skips malformed featured or moderator entries (D219,
`73b350b7`) and creates unseen actors only on the collection owner's host
(D226, `d958242c`); a ban window measured against the wrong clock
(`0d51f74c`).

**Feeds.** Every private feed was a 500 for its owner (`b0fbe483`); taken URLs
on create and copy are refused with a message (D681, `3273e200`, `e9174180`);
copying redirects to the owner's list (D721, `5b927bbf`); a failed remote feed
join leaves no membership behind (D682, `41995584`); a feed with a null owner
publishes an empty moderators collection (D170, `cf2fa862`).

**Users and authentication.** Malformed password hashes are a failed login,
not a 500 (`b93da714`); an ipinfo.io outage no longer fails logins
(`b4f6805d`); messaging someone again reuses the conversation (D748,
`4af2baa5`); user 1 cannot delete itself (`00bdbe1e`); NULL `verified` is
treated as unverified (`7cec275d`).

**API.** Besides the contract changes in 2.4: three reply verbs refused
unauthorised callers (`a6f8f081`); API image alt text (`63e81deb`); many
missing-id 500s replaced by 4xx.

**Admin.** The permissions page could delete a permission for good
(`84e7c4d3`); a Save that closed the instance (`d665128a`); the admin reports
search box did nothing (`18a3b754`); role names spelled once as constants and
matched by name (D965, D481, `c7b5c457`, `53b7d247`); a domain entered on the
federation page is normalised once (D929, `3440fe0a`); blocking an unknown
domain or this instance's own domain is refused (D581, `ba8b6f20`).

**Background tasks.** Tasks use their own sessions (D312, `636fe0c6`); a
community with no instance is treated as undeliverable (D331, `f3f30b4a`);
`refresh_instance_chooser` uses a SAVEPOINT per domain (D364, `7d636cfe`);
stats tasks commit per row with consistent lock order (D342/D343,
`cf429976`); a retried new-post fan-out skips users already notified (D278,
`7bbc9df0`); S3 clients are closed on failure (D363, `39913095`).

**Uploads and media.** Besides the size and format changes in 2.3: uploads no
longer reset Pillow's process-wide pixel ceiling (D467, `9a1af51a`); an SVG
banner was a 500 (`d5bcb0f0`); no post image was ever purged from the CDN
(`1d0f9c2d`); thumbnails on a URL change obey the remote-image caching setting
(D289, `902a24de`); the PeerTube embed rewrite is anchored (R251,
`9dc5c90a`).

**Search.** Logged-out search never returns NSFW (`986c32ce`); a URL search
searched nothing (`5d7d5303`); a searched reply reordered its thread
(`f1297dea`).

**NNTP.** The NNTP server's image fetch goes through the SSRF guard and the
pinned client (`024d0736`); NNTP login goes through `User.check_password`, so it strips
passwords like every other path (`6cbc0f7b`).

**Plugins.** A `before_post_create` plugin's returned title and content are
now used, and a handler returning `None` no longer nulls the data (D811,
`ed5d20a5`); `load_plugins` hands out a copy of the registry (D812,
`82291609`); `FLASK_DEBUG=true` stopped every plugin from loading
(`b5e941a8`); `/webhook` requires `WEBHOOK_SECRET` (see 2.2, `5a912e27`).

### Late fixes

Residues of earlier fixes, found after the register was closed:

- A defederation-list download that fails (unreachable, not 200, not a
  usable list) is logged and leaves that subscription's bans untouched;
  only a genuinely empty list clears them (D378 residue, `21d6d112`).
- A remote feed join that fails after its Follow was sent also sends the
  Undo{Follow}, so the peer does not keep us subscribed (D682 residue,
  `b2ba3b4b`).
- Following a user you already follow, or have asked to follow, is a no-op:
  no second row, count or Follow (R265 residue, `cb9baeff`).
- On a private instance a member with no RSS token gets one wherever a
  page renders its RSS link, not only on the front page (R219 residue,
  `7ba5dde3`).
- A moderator or feed owner listed after the 50th collection entry keeps
  the role on refresh; already correct, now pinned by tests (D225 residue,
  `866ba051`).
- Passkey login options give an unknown name, or an account with no
  passkey, one stable decoy credential, so they cannot be told apart from
  an account with one (D888 residue, `0cf94243`).
- The community page and `do_subscribe` check bans through
  `user_banned_from_community` alone; the bulk-import result reports every
  ban as `user_banned` (D995 residue, `b0dad48a`).
- Inbound activity log labels: a Page arriving through the chat fallback is
  logged as Create (D130 residue, `4f13d90f`); a Mastodon-style Block is
  logged as User Block, a new `APLOG_USERBLOCK` (D83 residue, `df34f48d`).
- Closed, not a defect: R223 residue (an Update omitting the More info Link
  clears it), because an Update replaces the object.

### Fixes in the feature tracks

The feature tracks after 2026-10-01 were built test-first, so most of their
`fix` commits correct a new feature during review. These are listed with
their features in 2.3 and 2.5. Fixes to code that predates the tracks:

- **Request-path sleeps.** Resolvers, inbox processing and relay detection no
  longer sleep and retry in-line. Only housekeeping tasks retry (`7bb92a8a`,
  `b5cf4322`, `267a483c`, `140c4cae`). Background image processing keeps its
  retry (`848977cb`).
- **Celery.** Background work runs in its own worker, so it can never starve
  the inbox (`0346d103`). Each task starts without the previous task's
  `g.site` (`6bbfea07`). The sync tasks use the background queue
  (`3ca3befc`).
- **Backfill.** An entry that cannot be stored is skipped instead of ending
  the whole backfill (`ff261f9e`). A community with no outbox still gets its
  featured posts and reply counts (`01ce65c8`).
- **Nodeinfo.** A host whose nodeinfo fails is asked once a day, with a
  5-second cap. A nodeinfo 2.x href is followed only when it is https on the
  instance's own host (`1a0c7fc0`, `f95d3e44`).
- **Translations.** Stale fuzzy suggestions whose placeholders belong to
  another string were cleared (`54c61620`, `a8428597`).
- **`fastapi_server`** starts and stops with a lifespan handler, not
  `@app.on_event` (`d5a225d6`).
- **Orphan sweep.** `remove_orphan_files` read `File` once per file under
  `media/users`, about an hour on a large instance, which ran the weekly job past
  its timeout; it now reads the names once (`2332dbc5`). It also sweeps pictures
  in `media/posts` that deleted posts leave behind (`f2182a54`).
- **The web container's start** no longer depends on data.lemmyverse.net
  (`b0f9a296`, 2.2.1).
- **Test suite.** Tests that named a real domain (`mastodon.cloud`) resolved it
  on every run, and hung when the test host's DNS stalled; they use a reserved
  name, which the suite answers itself (`4877bacf`). A crashed parallel worker
  reruns the whole run, and heavy tests run alone after the parallel phase
  (`674f03b6`, `bd5644c5`).

### Fuzzing track

Atheris now fuzzes every external input surface: 72 targets on 12 surfaces,
run in seven tiers. The tiers are signatures and transport, ActivityPub JSON,
markdown with ReDoS checks, URLs and discovery, NNTP, the API and imports, and
image decoding (`2026-10-13-fuzz-all-input-surfaces-design.md`). Each finding
was fixed test-first, one commit per defect, with its minimised input kept in
the corpus the suite replays. Where a fix needed a choice about behaviour, the
owner ruled; those rulings are the limits listed in 2.3. Typical fixes:

- malformed Signature, Content-Length and Accept headers are a format error
  rather than an exception (`efe47bf3`, `ffdbcdff`, `22d7436c`, `48b76c87`);
- out-of-range dates are refused (`68899f82`, `0c609fc5`, `0cffcb2f`);
- a shared-inbox body that is not a JSON object is refused (`f664facd`);
- a host longer than 253 characters is refused before IDNA (`f76370bd`);
- a gallery reads at most 20 attachment entries (`9a05d2bc`);
- markdown nested past markdown2's recursion limit renders nothing
  (`0d9b6da6`);
- the linear-time regex rewrites listed in 3.1;
- a URL, User-Agent or webfinger resource that furl or urlparse cannot read
  is refused instead of raising (`89430186`, `25d16561`, `50fbcf28`);
- `links_with_parens` moves a `)` without flattening the link's markup
  (`0d34a942`), and `shorten_url` honours its `max_length` (`872a81c1`);
- sixteen NNTP handler fixes: wrong argument counts and malformed dates get
  `501`, a line that is not UTF-8 closes with `205`, AUTHINFO with a NUL is a
  failed login, and the handler returns when the client closes the
  connection (`c15a67d0` to `05f491f0`);
- a compressed body in an encoding the capped reader does not decode is
  refused as such whatever length it declares (`1b82883b`).
- a banner refused on the community edit form, by its owner or an admin, no
  longer leaves the community naming an old icon (or, on the admin form, an
  old banner) whose files were already deleted: the old files go only once
  the new images are committed (`5eef3f16`, `ed938962`).

---

## 5. New features

- **Payments.** Admins connect Stripe, define offers and read a payment ledger at
  Admin > Payments, with the `manage monetization` permission. Credentials are encrypted in
  the database under `PAYMENT_SECRETS_KEY`. Members pay and cancel at `/donations`, and a
  member can hold several subscriptions, one per group. Admins can also add links to
  Patreon, Ko-fi or anywhere else, and logged-out visitors can make one-off donations. See
  `INSTALL.md`. Spec
  `docs/superpowers/specs/2026-11-05-monetization-infrastructure-design.md`.
- **BTCPay Server.** Admins connect their own server at Admin > Payments and take crypto for
  donations and prepaid periods, which members renew by paying again. It can be offered on an
  onion domain if it has an `.onion` address of its own. See `INSTALL.md`.
- **PayPal.** Admins connect it at Admin > Payments, for subscriptions and one-off donations. It
  hasn't been run against PayPal yet, so try the sandbox first. See `INSTALL.md`.
- **Private and paid domains.** "Require login to browse" is now per domain, and refuses everything
  not on a short list, so a private site no longer answers logged-out API calls. **An integration
  that reads a private instance's API without logging in must now log in.** A domain can charge a
  first subscription fee at registration, and restrict a member whose subscription has lapsed. See
  `INSTALL.md`.
- **The Donate page is a CMS page.** `/donate` is seeded and edited at `/admin/pages`, and any
  CMS page can opt in to a header link, a footer link and the sidebar. Spec
  `docs/superpowers/specs/2026-11-05-cms-donate-page-design.md`.
- **A media cap.** Local copies of remote posts' images last `remote_image_cache_days`
  from the post's arrival; relay expiry runs hourly so its backlog drains. Both run in
  `expire_old_media.sh` (2.2.1).
- **Third-party client apps** (plan `docs/superpowers/plans/2026-11-07-api-completeness.md`).
  Members can use, against a PieFed account:
  - Mastodon apps: Tusky, Mastodon's own, Phanpy, Elk and others;
  - Pixelfed's official app, PixelDroid and Fedilab, with direct messages and stories;
  - NewPipe, and PeerTube's own app with uploads;
  - Jerboa;
  - AntennaPod, which syncs subscriptions and listening through gpodder.net sync;
    Kasts and gPodder speak the same API but have not been tried.

  Members manage the apps and app passwords at `/user/settings/apps` (2.3, 2.4). The
  harness `interop/apps/` drives 21 apps; every one that can reach PieFed logs in, reads
  and posts, as its matrix expects.
- **Chat requests, muting, hiding and pictures**, for the web chat and apps alike (2.3).
- **Local boosts, mutes and Web Push**, for apps (2.3).

There are 146 `feat` commits. The first twelve came during the coverage
campaign:

| Commit | Feature |
|---|---|
| `2bf6c3cb`, `61c1acff`, `a5885033` | Helpers for reading Announce activities, boost cache entries, and boost recording and removal |
| `f1d8cb26` | Ingest boosts of microblog posts from followed accounts |
| `a1f5eb35` | Handle Undo of microblog boosts (boost counts could previously only rise) |
| `b8bcd7ce` | Posts boosted by followed accounts appear in the feed |
| `f4d35ce9` | An ActivityPub visibility classifier |
| `65c524b1` | Refuse followers-only and direct content at ingest |
| `0254fdec` | Resolve unknown remote handles at `/u/<user@host>` for signed-in HTML requests |
| `6659cdd4` | Admin tool to follow accounts from a Mastodon server's directory |
| `5f531bf7` | Configurable redirect policy |
| `df3d3236` | A read-only report of actor rows whose profile host disagrees with their domain |

Features finished or added through `fix` commits under owner rulings:

- Event "More info" link: stored, rendered, federated and ingested (R223,
  `d8f7a9f5`).
- Reply keyword filtering, with the comment API `filtered` flag (R168,
  `43512918`).
- Outbox paging (D173, `ec61f68e`), with paged backfill (`055e0937`).
- Chat reports federated as a `Flag` (D761, `9390ec0a`, `dbc61619`).
- Quote authorisation records (R205, `35abcf19`).
- Private-instance RSS by member token (R219, `619de402`, `c1f96cb0`).
- "Request declined" state for refused follows (R265, `9d488574`).
- NSFL control for local communities (R203, `327fcddd`).

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
| Interface | | Italian, Korean and Russian translations offered (`15dc82fc`) |
| Media cap | | Remote post images evicted by age, hourly relay expiry, orphan sweep of `media/posts` |
| OAuth and read layer (W0) | `2026-11-07-mastodon-client-api-design.md` §3.1, plan W0 | OAuth 2 server, Authorised apps page, `app/reads/` under `/api/alpha` |
| Mastodon (W1) | `2026-11-07-mastodon-client-api-design.md` | Accounts, timelines, statuses, notifications, search, media, follows, local boosts, mutes, Web Push, admin scopes |
| Pixelfed (W1.4) | `2026-11-10-pixelfed-app-design.md` | Pixelfed's routes for its three apps, direct messages on chat (requests, mute, hide, pictures), stories |
| PeerTube (W2) | `2026-11-09-peertube-read-api-design.md`, `2026-11-09-peertube-login-design.md`, `2026-11-10-peertube-upload-design.md` | Read API, 480p and 360p renditions, login, subscriptions, ratings, history, notifications, resumable upload |
| Lemmy (W3) | `2026-11-10-lemmy-v3-design.md` | `/api/v3` for Jerboa, member endpoints |
| gpodder (W4) | `2026-11-12-gpodder-sync-design.md` | App passwords, devices, subscriptions that join communities, episode actions that mark posts read |

---

## 6. Internal and developer changes

**Import cycles.** The circular import between `community.routes` and
`activitypub.routes` was broken (`5ab17738`), then six further cycles
(`a3f0ee6c`, `c09958a4`, `8e980532`, `ad9ec615`, `16e931a3`, `92b20374`) and
the cycles under `app.shared` and `config` (`8060d1c6`, `9f03cc02`). The test
conftest no longer pre-imports `app.activitypub` to hide them (`2ecb78b9`).
`tests/test_import_order.py` imports every module under `app/` first in a
fresh interpreter, so a new cycle fails the suite.

**Inline-import ratchet.** Of 216 function-level imports in `app/`, 131 were
hoisted to module level (the `refactor: hoist inline imports` series) and 85
were kept with a tag explaining why: 69 `# cycle:` and 16 `# lazy:`.
`tests/test_no_inline_imports.py` fails on an untagged function-level import
and on any increase in the kept count (`e5182371`).

**Mutating-GET ratchet.** `tests/test_mutating_get_routes.py` fails when a GET
route writes, directly or through a shared helper (`e2de0f5c`, `7e38e67d`).

**One moderation predicate.** `can_moderate` in `app/utils.py` states the
moderation rule once; `can_mod_post` delegates to it (`a8075d70`). Similarly,
one helper decides community bans (`ab8d9ade`), one names the user-1 ban
exemption (`8ab7ee7a`), one parses the Signature header (`69ced92a`), and one
decides redirect safety (`0c923ae2`).

**Other refactors.** 841 call sites moved off SQLAlchemy's legacy
`Query.get()` (`c71d66b9`); request wiring moved into the app factory
(`7288fa13`); dead code and debugger stubs removed (for example `3761c054`,
`2373e85d`).

**Test harness.**
- `run_tests.sh` runs the suite against disposable database containers
  (`24a92f74`, `5a591d79`); `compose.test.yaml` builds the Dockerfile's
  `test` stage.
- `tests/factories.py` provides model factories (`24a92f74` onward).
- The suite runs on four pytest-xdist workers, each with its own database and
  Redis databases (`5b9b0fe1`), with `xdist_group` load groups honoured even
  for plain `-n` runs (`e3754d15`). Tables are cleared by deleting rows rather
  than truncating (`89052561`).
- Per-module coverage floors in `coverage_floors.ini`, checked by
  `tests/test_coverage_floors.py`, only move up.
- The test database runs with `jit=off`. The suite's tables are never
  analysed, so the planner JIT-compiled small queries at about 2.7 s each
  (`compose.test.yaml`).
- `run_tests.sh` also runs the client-side tests for `live_feed.js` with node
  when it is installed, and fails below 100% coverage.
- `tests/fuzz/` holds the atheris targets (`tests/fuzz/targets/`, one module
  per surface, collected into `REGISTRY`) and the manifest of input surfaces
  (`tests/fuzz/surfaces.py`). Three gates run in the suite:
  - a registry test fails when a surface has no target or a target has no
    corpus;
  - the corpus replay fails an input that runs over its target's CPU-time
    budget, scaled to the machine's measured speed;
  - the smoke script's own test.
  `python -m tests.fuzz.smoke --seconds N` runs every target as a campaign.
  `tests/README.md` describes it all (`fa97931e`).
- The suite collects 23,526 tests.

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

- **v1.8.10's code is covered to the floors.** After the rebase 22 modules were
  below their floor in `coverage_floors.ini`; the tests added since
  (`7a48c72a` and the fixes above) bring all 125 back, and every line and
  branch added since `2aaea7e2` is executed.
- **v1.8.10's admin analytics follow the onion rule (owner ruling
  2026-11-01).** `/admin/votes`, `/admin/votes_for_noone`,
  `/admin/serial_downvoters`, `/admin/high_posts_per_day`,
  `/admin/no_link_diversity` and an account's `/u/<actor>/voting_patterns`
  leave out every row naming an account hidden here, as voter or target
  (`accounts_shown_here_sql`, a `/* shown here: <alias> */` marker in each
  SQL file); the onion still sees its own. `/admin/votes`' hour-long cache is
  kept per set of hidden accounts, so the onion's is never served on the
  clearnet; its refresh clears every storefront's.
- **D354 stays deliberate.** Per-row commits inside loops are kept as ruled;
  this is the only item the register still lists as deliberate.
- **Closed as not defects:** D203 (restore's cross-post recompute from NULL
  is correct) and D599 (nothing memoizes community notification
  subscriptions).
- **Won't-fix, recorded:** D157 (confirmed unreachable over ActivityPub since
  D156, pinned by `f9e55630`), D450 (dead guard removed, `098cada5`).
- **`SIG_REQUIRED_HEADERS_ENFORCE` defaults to off.** The code default stays
  `0`, so that an operator can review the logged warnings before refusing
  under-signed requests.
- **Domain bans match one spelling only.** A ban on `bücher.example` does not
  match `xn--bcher-kva.example`, or the reverse.
- **A GIF with many frames is not capped.** The 50 MP limit applies to each
  frame, not their total. A local icon upload of 400 frames at 2000x2000
  costs about 30 s and 3.3 GB. The owner chose not to add a frame cap. Remote
  thumbnails and federated images are bounded by the 16 MP cap, which counts
  every frame.
- **What the client APIs do not serve yet.**
  - Mastodon: emoji reactions; admin routes, though the admin scopes exist;
    reblogs as entries in followers' home timelines.
  - Lemmy: moderation and admin endpoints (W3b, ruling L2).
  - Pixelfed: group chats.
  - PeerTube: the Private and Password-protected privacies (ruling U1). TubeLab is
    unsupported (P4): its login needs nodeinfo to say `peertube`, and it sends a
    user agent other apps share.
- **No web buttons for boosts or mutes, and no pictures from the web chat's form.**
  Apps can do all three.
- **A gpodder lockout is per account.** Someone who knows an account name can lock
  it out of podcast sync for up to an hour from an address it shares, such as the
  onion's common bucket: the price of checking the limit before any password.
- **Converted form buttons are not browser-verified.** The Join, Leave,
  notification-bell and feed buttons that changed from links to POST forms
  are covered by tests but have not been checked by hand in a browser.
  Reviewers should exercise them, with and without JavaScript, before
  release.

# Proactive sync of PeerTube channels and Castopod podcasts

Status: design approved in session (2026-10-05). Fork-only work, follows interop D24 and the discovery
seeding design (`2026-10-03-discovery-seeding-design.md`).

## Problem

PeerTube and Castopod content works, but it is second-class:

- A `DiscoveryEntry` fetches nothing until a user opens or joins it. Opening creates the community and
  runs a one-time backfill of up to 50 items (`retrieve_mods_and_backfill`, `app/community/util.py:150`).
- After that, new posts arrive only through a local follower's Follow. A community someone opened but
  nobody joined never updates again.
- The admin pre-load (`app/discovery/preload.py`) works around this by following as user 1, so user 1
  looks subscribed to every pre-loaded channel.
- Nothing helps a reader find video and podcast content among discussion posts, and search knows only
  what is already stored here.

## Goals

- Keep an admin-set number of channels per remote host proactively backfilled and kept fresh, so nobody
  has to "open to join" to see content.
- Give videos and podcasts their own feed tab and search filters, and an opt-in search of the wider
  PeerTube network.

Success: once an admin sets a non-zero per-host count, recent PeerTube and Castopod posts appear in All,
in the Videos & Podcasts tab and in post search without anyone opening a channel. New posts follow
within minutes (push) or at most a day (poll).

Non-goals: Mastodon and Pixelfed entries (people, not channels; unchanged); a total cap across hosts;
live search of Castopod (its index is a file export with no search API); "people and channels to
follow" panel (still future work (c) of the seeding design).

## Decisions (owner rulings, 2026-10-05)

1. **Scope of N:** per remote host. Each PeerTube or Castopod host contributes its top N channels by
   followers. No total cap.
2. **Freshness:** hybrid. The instance actor (`/actor`) follows each synced channel, so the remote
   pushes new posts, and a daily outbox poll fills gaps from lost deliveries or refused Follows.
3. **Drop-out:** a channel that leaves the desired set is unfollowed (`Undo Follow` from `/actor`) and
   no longer polled. Its community and posts stay and age out under normal retention. Local members
   keep their own follows.
4. **Pre-load:** replaced. The pre-load form, `preload_discovered_communities` and `PRELOAD_USER_ID`
   are removed. User 1's existing memberships are left alone: `do_subscribe(admin_preload=True)` is
   shared with the lemmyverse pre-load and the htmx join, so discovery pre-loads cannot be told apart
   from manual joins.
5. **Feeds:** synced posts stay in All as backfilled posts already do. A new "Videos & Podcasts" feed
   tab shows only PeerTube and Castopod posts. Popular is unchanged and stays score-driven.
6. **Search:** local filters plus an opt-in external search. The search page has a user toggle,
   "Include results from the wider network", which defaults to off. An admin kill-switch can hide it.
7. **State storage:** a new `discovery_sync` table, not columns on `Community`.
8. **Default:** `discovery_sync_per_host = 0`. An upgrade never starts following channels until an admin
   chooses a count.
9. **Coverage:** 100% line and branch coverage of all new code and of every changed line in existing
   modules (see Testing).

## Data

### Settings (`Settings` table, `get_setting` / `set_setting`)

| Name | Type | Default | Meaning |
|---|---|---|---|
| `discovery_sync_per_host` | int 0–50 | 0 | Channels kept synced per remote host. 0 turns sync off. |
| `discovery_sync_platforms` | list | `['peertube', 'castopod']` | Platforms that sync. |
| `discovery_external_search` | bool | True | Whether the search page offers the external toggle. |

### Table `discovery_sync`

| Column | Type | Notes |
|---|---|---|
| `community_id` | int PK, FK `community.id` ON DELETE CASCADE | The synced community (a Castopod podcast's twin). |
| `entry_id` | int FK `discovery_entry.id` ON DELETE SET NULL, nullable | The entry that selected it. The entry can expire first. |
| `follow_target` | String(255) | Actor URL the Follow went to: the PeerTube channel, or the Castopod Person. |
| `follow_uuid` | String(36), nullable | uuid in the Follow id `SERVER_URL/activities/follow/<uuid>`. Null if no Follow was sent yet. |
| `follow_state` | String(10) | `pending`, `accepted`, `rejected`, or `none` (instance offline, Follow not sent yet). |
| `followed_at` | DateTime, nullable | When the Follow was last sent. |
| `last_polled_at` | DateTime, nullable | When the last poll finished. |
| `last_error` | String(255), nullable | Last poll or federation error, cleared on success. |
| `created_at` | DateTime | |

Migration: create the table only. No data migration.

## Reconcile

`app/discovery/sync.py`, `reconcile_sync()`. It runs daily as the CLI command `flask sync_discovery`,
called from `daily.sh` after `refresh_discovery`, and on demand from the admin "Sync now" button
(Celery task `reconcile_sync_task`).

1. **Desired set.** If `per_host` is 0 or no platform is enabled, the desired set is empty. Otherwise,
   for each host, take the top `per_host` entries by `followers` desc, then `name`, among
   `DiscoveryEntry` rows with `kind == community`, platform enabled, `nsfw == False`, host not
   `instance_banned`. Entries whose community exists here but is banned or deleted are skipped.
2. **Drop.** Each row whose community is not in the desired set is dropped: send Undo (below), then
   delete the row. The community and its posts stay.
3. **Add.** Each desired entry with no row is added, up to **10 per host per run**, so a larger N ramps
   up over several days:
   - `find_actor_or_create(entry.actor_url, community_only=True)`. Not a `Community` means skip and log.
   - If the community has no posts, `queue_backfill(community.id)`.
   - Insert the row, then send Follow (below).
4. **Retry.** Rows in `pending` with `followed_at` older than 7 days, or in `none`, get their Follow sent
   again.
5. **Isolation.** Each host runs in its own SAVEPOINT (D364 pattern). One host's failure is logged and
   rolled back. It never stops the other hosts.
6. **Poll.** After reconcile, enqueue `poll_synced_community(community_id)` for every row, on the
   `background` queue. Polls of the same host are staggered by `countdown` so that one host sees at
   most one poll every 2 seconds.

### Poll

`poll_synced_community(community_id)` walks the outbox (`follow_target`'s outbox for Castopod,
otherwise the community's) newest-first with `_walk_outbox_pages`. It ingests through the same per-item
path as `retrieve_mods_and_backfill` and stops at the first item already stored or after 50 items
(`BACKFILL_ITEMS`). It sets `last_polled_at` and clears `last_error` on success. On failure it sets
`last_error` (truncated to 255) and logs. There is no in-task retry (D738/D775): the next daily run
retries. An entry that cannot be stored is skipped, as in the backfill (8a049a57e). A row whose
community has gone is deleted.

## Federation (instance actor)

- **Follow:**
  `{"actor": SERVER_URL/actor, "to": [target], "object": target, "type": "Follow", "id": SERVER_URL/activities/follow/<uuid>}`,
  signed with `g.site.private_key` and key id `SERVER_URL/actor#main-key` via `send_post_request` to
  the target's inbox. `target` is `community.public_url()` for PeerTube and the podcast's Person actor
  for Castopod. If `community.instance.online()` is false, the state is `none` and nothing is sent. The
  state is set to `pending` and `followed_at` to now when sent.
- **Accept** (`app/activitypub/routes.py`, Accept branch). A new branch runs before the requestor-user
  lookup. When the Accept's object is a dict whose `actor` is `SERVER_URL/actor`, or a string whose last
  path segment matches a row's `follow_uuid`, it sets the row to `accepted` and logs success. It creates
  no `CommunityMember` and does not change `subscriptions_count`. No matching row means log ignored.
- **Reject:** the same match sets `rejected`. The poll keeps running. The Follow is not sent again
  unless the row is dropped and re-added.
- **Undo:**
  `{"type": "Undo", "actor": SERVER_URL/actor, "object": <the stored Follow, rebuilt from follow_uuid and follow_target>, "id": SERVER_URL/activities/undo/<new uuid>}`,
  with the same signing. It is best-effort: a transport failure is logged and the row deleted anyway. It
  is skipped when `follow_uuid` is null.
- **`/actor/inbox`:** a new POST route that hands off to the same handler as `/inbox`, because the
  actor document advertises it and some peers ignore `sharedInbox`.
- **Inbound content:** Announce and Create ingest have no local-member gate, so no change is needed.
  Castopod Creates from the Person already route to the twin through `podcast_route_for`.
- **`/actor` document:** unchanged (`manuallyApprovesFollowers: True`).

### Spike before planning (throwaway)

Send a Follow from `/actor` to one real PeerTube channel and one real Castopod podcast. Record whether
each Accepts and then pushes new content to `/inbox` or `/actor/inbox`. The owner approves the target
hosts before it runs. A platform that refuses an `Application` follower falls back to poll-only: its
rows stay in `none`, the reconcile never sends it a Follow, and that is recorded in this spec.

## User-facing surfaces

- **Media predicate.** One helper, `media_community_clause()` in `app/discovery/sync.py`, is true when
  the community's `Instance.software`, lowercased, is `peertube` or `castopod`. The feed, post search
  and community browse all use it.
- **Home feed.** `view_filter == 'media'` in `home_page` (`app/main/routes.py`), with the same private
  and low-quality guards as `popular`. It works for anonymous users and gets a "Videos & Podcasts" item
  in `_view_filter_nav.html`.
- **Post search** (`app/search/routes.py`):
  - The "Videos & Podcasts only" checkbox (`media=1`) applies the media predicate.
  - The "Include results from the wider network" toggle (`external=1`, off by default) renders only
    when `discovery_external_search` is true. The server ignores `external=1` when the setting is false.
  - With the toggle on and a non-empty `q`, the page calls `app/discovery/external_search.py`,
    `search_videos(q)`. That function:
    - GETs `https://sepiasearch.org/api/v1/search/videos` with `search=q`, `count=10`, and a 3-second
      timeout through the pinned client (R162).
    - Caches results for 10 minutes per normalised (stripped, lowercased) query.
    - Drops videos already stored here (matched by `Post.ap_id` against the video URL), videos on banned
      hosts, and NSFW videos (always NSFW-free for anonymous users, following D798; otherwise the user's
      `hide_nsfw`).
  - The results render as a "From the wider network" block below the local results.
  - Timeouts, HTTP errors and bad JSON hide the block and log at info level. They are never a 500.
  - Both flags travel in the query string and survive pagination.
- **Opening an external result.**
  - Logged in: a POST with CSRF goes to `/discovery/video/resolve`. It resolves through the existing
    authenticated resolve path (PERM-1) and redirects to the local post. If it fails, it flashes "couldn't
    reach that server" (D720) and returns to the search.
  - Anonymous: the result is a plain link to the remote video URL.
- **Community search and browse.** Communities whose instance is PeerTube or Castopod get a platform
  badge. `list_communities` gains a `platform` filter (All / PeerTube / Castopod) using the same
  predicate. The discovery fallback is unchanged.
- **Admin Discovery tab** (`/admin/federation/discovery`, permission `change instance settings`). The
  pre-load form is replaced by:
  - a form with per-host count (0–50), platform checkboxes, and the external-search switch
  - a **Sync now** button (POST + CSRF) that enqueues `reconcile_sync_task`
  - a status table: one row per synced community, showing host, platform, follow state, `followed_at`,
    `last_polled_at` and `last_error`

## Error handling summary

| Case | Behaviour |
|---|---|
| Host banned, entry turns NSFW, platform disabled, community banned or deleted | Dropped at next reconcile: Undo, delete row. |
| `per_host` set to 0 | Every row dropped. |
| Remote offline at Follow time | State `none`, retried next run. |
| No Accept within 7 days | Follow re-sent. |
| Reject | State `rejected`, poll continues. |
| Undo transport failure | Logged. Row deleted anyway. |
| Poll failure | `last_error` set, retried next daily run. |
| One host fails during reconcile | That host's SAVEPOINT is rolled back. The other hosts continue. |
| External search failure | Block hidden, info log. |

## Testing

TDD: failing test first, one commit per unit. Removing the pre-load includes deleting its tests and the
`preload_user_can_subscribe` callers.

**Coverage requirement: 100% line and branch coverage.**

- New modules (`app/discovery/sync.py`, `app/discovery/external_search.py`, and any new admin or view
  module) get `= 100` floors in `coverage_floors.ini`, measured with the existing branch coverage
  (`.coveragerc`, `branch = True`).
- Every added or changed line and branch in an existing module (`app/activitypub/routes.py`,
  `app/main/routes.py`, `app/search/routes.py`, `app/discovery/admin_views.py`, `app/discovery/forms.py`,
  `app/cli.py` or `app/discovery/cli.py`, templates' view logic) must be executed by a test. This is
  checked per commit against the coverage report for the changed lines.
- `# pragma: no cover` is not allowed in new code.

Test areas:

- **Reconcile:**
  - desired set: per-host N, ordering ties, NSFW, banned host, disabled platform, banned or deleted
    community
  - the add and drop diff, the ramp cap of 10 per host
  - the retry rules for `pending` older than 7 days and for `none`
  - SAVEPOINT isolation when one host raises
  - `per_host = 0`
- **Federation:**
  - the shape, target and signing key of the Follow and the Undo, for PeerTube and Castopod
  - offline instance
  - Accept and Reject, in both object forms, from the instance actor; no `CommunityMember` created; no
    matching row
  - Undo failure still deletes the row
  - `/actor/inbox` routing
- **Poll:** stops at a known item, the 50-item cap, an unstorable item skipped, `last_error` set and
  cleared, community gone.
- **Feed:** the `media` tab, anonymous and logged in, private-community guard, low-quality filter, nav
  item.
- **Search:**
  - the media checkbox
  - the external toggle: off by default, and both hidden and ignored when the admin setting is off
  - success, timeout, HTTP error, bad JSON
  - cache hit
  - dedup against stored posts, banned host, NSFW for anonymous and logged-in users
  - flags kept in pagination
- **External resolve:** POST without CSRF refused; success redirects; failure flashes; anonymous users
  get a plain link.
- **Admin:** settings form validation and save, Sync now enqueues, status table renders, pre-load form
  gone, permission refused for non-admins.
- **CLI:** `flask sync_discovery` runs the reconcile; `daily.sh` calls it.

The full suite runs at the end (`./run_tests.sh`).

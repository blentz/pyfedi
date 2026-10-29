# Seeding Pixelfed content into microblogs

Date: 2026-10-29
Status: spike done; owner rulings S1-S3 given in conversation; spec awaiting owner review

## 1. Problem

Mastodon posts reach `/c/microblogs` through relays (spec 2026-10-08), and PeerTube and Castopod content through the
discovery sync, where the instance actor follows the top N channels per host (spec 2026-10-05). Nothing brings
Pixelfed content: a Pixelfed post arrives only when a local user follows its author.

- Pixelfed has no relay support. Upstream issue pixelfed/pixelfed#4472 has been open since 2023-06, unanswered.
- Its public and network timelines need a logged-in account (`routes/api.php:203`, `auth:sanctum,api`), so they cannot
  be polled.
- Without a login it offers each server's opt-in account directory (`/api/landing/v1/directory`, which discovery
  already reads) and each account's Atom feed (`/users/<name>.atom`).

### Owner rulings (2026-10-29)

- S1. Pixelfed content lands in `microblogs`, beside relayed Mastodon posts. No separate community.
- S2. The instance actor follows the top N opted-in Pixelfed people per server, as the sync does for channels.
- S3. N is its own admin setting, `discovery_people_per_host`, default 0: nothing is followed until an admin sets it.

Out of scope: Mastodon people (relays bring them); suggesting people to users; Pixelfed Stories.

## 2. Spike results (harness, Pixelfed v0.15.2)

- Pixelfed accepts a Follow from `https://piefed.test/actor`, an `Application`: it stores the follower and sends an
  Accept. PieFed logs that Accept as `ignored` today, since no sync row holds its Follow.
- The followed account's next post (a 2-picture album) is pushed to PieFed's shared inbox and stored in `microblogs`
  with its gallery, with no local user following the author. Live delivery needs no change.
- `/users/<name>.atom` lists the account's last 10 posts; each entry's `<id>` is the post's AP URL. It answers 404 for
  a locked account and for one that turned its feed off. Pixelfed's outbox has no items, so the sync's outbox poll
  cannot be used.

Details: `.superpowers/spike-pixelfed/report/findings.md`, "Seeding Pixelfed content".

## 3. Design

### 3.1 Setting

`discovery_people_per_host`: an int from 0 to the sync's `MAX_PER_HOST`, default 0, read through one helper (as
`sync_per_host`). Shown on the discovery admin page beside `discovery_sync_per_host`, labelled "Pixelfed people to
follow per server".

### 3.2 Model

New table `discovery_person_sync`, the person-shaped twin of `discovery_sync` (whose key is a community):

| Column | Type | |
|---|---|---|
| `user_id` | FK user, CASCADE, primary key | the remote Pixelfed account |
| `entry_id` | FK discovery_entry, SET NULL | the directory entry that chose it |
| `follow_target` | String(1024) | the actor URL the Follow names, case kept |
| `follow_uuid` | String(36), indexed | the Follow id's uuid |
| `follow_state` | String(10) | `none`, `pending`, `accepted`, `rejected` (the sync's constants) |
| `followed_at`, `last_polled_at` | DateTime | |
| `last_error` | String(255) | |
| `created_at` | DateTime | |

One migration. A remote account an admin deletes or bans loses its row (cascade, or the reconcile drop), and its
actor URL goes into the existing `discovery_exclusion` table so it is never followed again.

### 3.3 Choosing people

`desired_people()`: `DiscoveryEntry` rows of kind person and platform `pixelfed`, not NSFW (people on servers
pixelfed.org files under Adult are NSFW), on hosts the instance does not block, not banned, deleted or excluded here;
the top `discovery_people_per_host` per host by followers, then name. Only accounts that opted into their server's
directory are ever entries, which is the consent signal discovery already relies on.

### 3.4 Reconcile

`reconcile_people()`, run where the channel reconcile runs (`flask sync_discovery`, daily), with the same shape:

- A row whose account is no longer wanted, or is banned or deleted here, is dropped: `Undo Follow` from `/actor`
  (best effort), then the row is deleted.
- A wanted account with no row: resolve the actor (`find_actor_or_create`), skip one that is local or on a hosted
  domain, write the row, send the Follow from `/actor` to the account's inbox (its shared inbox, as for any user), and
  queue its Atom backfill (3.6).
- A row `pending` for over 7 days, or `none`, gets its Follow sent again under the same uuid.
- The channel reconcile's per-run and per-host add caps apply to people as well.

### 3.5 Accept and Reject

`instance_answers.py` matches an Accept or Reject of an instance actor Follow by `follow_uuid` in `discovery_sync`;
it also looks in `discovery_person_sync`. Accept sets `accepted`, Reject `rejected` (no further Follows). Neither puts
any local user in a follow relationship: the instance actor follows, as for channels.

### 3.6 Atom backfill and gap fill

On add, and daily after that, `poll_synced_person(user_id)` reads `<actor url>.atom` (on the actor's own host only,
with the discovery fetch caps), takes each entry's `<id>` that is an https URL on that host and not already a post
here, and fetches it as any post URL is fetched by ActivityPub (signed GET, the inbound visibility and permission
gates), so it lands in `microblogs` as a pushed post does. At most 10 a poll (what the feed holds); a 404 or an
unreadable feed records `last_error` and stops that poll. Polls are spaced per host as the channel polls are.

### 3.7 Admin view

The discovery admin page lists synced people with their state (`pending`, `accepted`, `rejected`, `none`) and last
error, as it lists synced channels.

## 4. Tests

- Unit: selection (cap per host, NSFW and exclusions, order); reconcile add, drop with Undo, refollow; Accept/Reject
  matching for person rows; the Atom poll (good feed, off-host ids refused, 404, cap, known posts skipped); the
  setting's range; the admin form.
- Interop (`interop/test_pixelfed.py`): with the setting at 1, the reconcile follows erin from `/actor`, Pixelfed
  accepts, erin's next post lands in `microblogs`, and posts she made before the follow are backfilled from her feed.
- 100% line and branch coverage on new and changed lines.

## 5. Build order

1. Model, migration and setting.
2. Selection and reconcile (Follow, Undo, refollow) with Accept/Reject matching.
3. Atom backfill and daily poll.
4. Admin page.
5. Interop test.

# Discovery seeding for non-Lemmy platforms, and Castopod podcasts as communities

Status: design approved in session (2026-10-03). Fork-only work (interop spec D16/D17). Interop spec entry: D24.

## Problem

A new PieFed instance has no route to Mastodon, Pixelfed, PeerTube or Castopod content. Every built-in
seed points at the Lemmy side of the fediverse:

- the admin "pre-load communities" button and `flask populate_community_search` both read
  `data.lemmyverse.net/data/community.full.json` (Lemmy, PieFed and Mbin communities only);
- `publicize_community_task` asks eight Lemmy servers to resolve new local communities;
- the only PeerTube source is a blocklist (`peertube_isolation`).

Separately, interop spec D4 maps Castopod podcasts to communities, but the code treats a `Podcast`
actor as a person (G1) and files its episodes in `microblogs`.

## Goals

- **(a) Admin pre-load:** an admin can subscribe the instance to popular PeerTube channels and
  Castopod podcasts, as the lemmyverse pre-load does for Lemmy communities.
- **(b) Search index:** PeerTube channels, Castopod podcasts and opt-in Mastodon/Pixelfed people
  become findable in PieFed's community and people search without being subscribed to.
- **Castopod mapping:** a podcast is a community, an episode is a post in it, and the post's
  authors are the podcast's hosts plus each episode's guests.

Future, not in this work: **(c)** a "people and channels to follow" panel for users, fed by the same
data.

Non-goals: following people on users' behalf (instances can only subscribe to group-like actors);
real multi-author posts across PieFed; any bulk mirroring of directory data.

## Decisions

1. **Jobs:** (a) and (b) now; (c) recorded as future work.
2. **Pre-load scope:** PeerTube channels and Castopod podcasts only. Mastodon and Pixelfed accounts are
   people, so they go into search only.
3. **Sources** (research notes: session scratchpad `discovery-sources.md`; no source grants a bulk-use
   licence, so we sample politely and attribute):

   | Platform | Source | Consent |
   |---|---|---|
   | PeerTube channels | SepiaSearch `GET https://sepiasearch.org/api/v1/search/video-channels` (no auth; `url`, `host`, `followersCount`, `videosCount`; cannot sort by followers, so rank client-side) | Public publisher channels |
   | Castopod podcasts | Podcast Index API, only when the admin has entered an API key and secret. Keep podcasts whose `socialInteract` has `protocol: activitypub`; the actor URL comes from it | Publisher opts in via the feed tag |
   | Mastodon people | Top 20 servers from `https://api.joinmastodon.org/servers`, then each server's `GET /api/v1/directory?local=true&order=active&limit=80` | Only `discoverable` accounts are listed |
   | Pixelfed people | Pixelfed hosts from FediDB `https://api.fedidb.org/v1/servers?software=pixelfed`, then each host's `GET /api/landing/v1/directory` (404 = disabled by the admin; skip the host) | Only `is_suggestable` public accounts are listed |

4. **Podcast Index credentials:** the admin UI has write-only fields for the API key and API secret
   (requests are signed with SHA-1 of key + secret + unix time). They are stored as site settings, never
   rendered back (the page shows "configured" / "not set"), and never logged.
5. **Castopod authorship:** a podcast actor gets a `User` row (the technical owner) and a `Community` row
   with the same actor URL (`ap_profile_id` is unique per table, not across tables). `Post.user_id`
   stays the podcast actor so the per-author machinery keeps working; the page and API show **credits**
   instead.
6. **Credits:** an ordered list `{name, role, image, profile_url, user_id}` from the podcast's RSS
   `podcast:person` tags: channel-level tags are hosts, item-level tags (`role="guest"`) are guests; the
   item is matched to the episode by link or GUID. A credit whose `href` resolves to a fediverse account
   (https only, webfinger or actor fetch with the usual guards) links to that PieFed `User`.
7. **Storage:** a new table `discovery_entry`, and a `Post.extensions` JSON column (the first piece of
   D8's content-kind layout), both added by one fork migration revising the current head. Credits live
   in `Post.extensions["podcast"]["credits"]`; the API exposes `post.extensions.podcast.credits`. The
   lemmyverse JSON files and their code stay untouched.

## Design

### Components

- **`app/discovery/`** (new package):
  - `peertube.py`, `castopod.py`, `mastodon.py`, `pixelfed.py`: one fetcher each, returning normalised
    entries `{kind: community|person, platform, actor_url, name, host, avatar, followers, nsfw, source}`.
  - `refresh.py`: runs the fetchers; filters out banned instances, blocked domains, hosts on the
    PeerTube isolation list and names failing `is_bad_name`; tags NSFW; caps entries per host and per
    source; upserts into `discovery_entry` (unique on `actor_url`); deletes entries not seen for 30
    days.
- **`discovery_entry` table:** `id`, `kind`, `platform`, `actor_url` (unique), `name`, `host`,
  `avatar_url`, `followers`, `nsfw`, `source`, `first_seen`, `last_seen`.
- **Scheduling:** `flask refresh_discovery` CLI command, called from `daily.sh` (the existing daily
  cron). Never runs on a request.
- **Admin page** (where the lemmyverse pre-load lives): Podcast Index key/secret fields; a "Pre-load
  channels & podcasts" control (choose N and platforms, preview, then subscribe); attribution text
  naming every data source.
- **Search:** community search (`/communities`) and people search (`/search?search_for=people`) fall back to `discovery_entry` when nothing local
  matches. Results show a platform badge and a join/follow button that resolves the actor through
  `find_actor_or_create`.
- **Castopod mapping:**
  - creating a `Podcast` actor also creates its `Community` row;
  - episode Notes from that actor are filed in its community instead of `microblogs`;
  - the episode audio/cover feature (WP-C) keeps working;
  - credits are parsed from the RSS feed and stored on the post;
  - the post byline shows "Hosted by A, B · with guest C" instead of the owner;
  - every actor lookup that does not pass a type hint (`Accept`, `Undo`, inbox routing,
    `find_actor_by_url` callers) is audited so it resolves the right row for a dual actor.

### Limits, errors and privacy

- **Network:** every fetch uses `get_request` (SSRF guards, redirects off) with a short timeout.
  Per-run budgets: PeerTube at most 5 pages; Mastodon at most 20 servers × 1 directory page of 80;
  Pixelfed at most 20 servers × 2 pages; Podcast Index at most 200 results. Calls to the same host are
  spaced well under each published rate limit. A failing source is logged and skipped; its previous
  entries remain until the 30-day expiry.
- **Consent:** people come only from opt-in directories. Nothing is fetched from a banned instance, a
  blocked domain or an isolated PeerTube host.
- **NSFW:** entries are tagged and shown only to viewers whose NSFW setting allows them, using the
  community-search rule.
- **Untrusted data:** names and descriptions are stored as plain text, capped and escaped on render.
  Avatar URLs must be https and pass `url_is_storable`. Nothing is downloaded until someone follows or
  joins.
- **RSS for credits:** fetched with the same guards, capped at 2 MB, parsed with the stdlib XML parser
  (expat; no external entity resolution). A broken or missing feed leaves the post without credits and
  the byline falls back to the podcast's name.
- **Pre-load:** runs as a Celery task; idempotent (communities already known are skipped); each
  subscription goes through the existing community-join path.
- **Visibility:** discovery adds no content of its own. Castopod episodes keep their visibility rules
  (interop D6/D7). The visibility test suite stays green.

### Testing

- **Fetchers:** fixture JSON through `http_mock`, no live network. Cover normalisation, opt-out
  handling (`discoverable=false`, Pixelfed directory 404), blocklists, budgets and paging, Podcast Index
  signing (checked against a computed value), and a failing source keeping existing entries.
- **Refresh:** idempotent upsert, 30-day expiry, cleaning of names and URLs.
- **Admin:** credentials saved but never rendered back; pre-load preview honours N and the platform
  filter; subscribe calls the join path once per new community and skips known ones; attribution shown.
- **Search:** local results first, then discovery fallback; NSFW hidden by default; join/follow resolves
  the actor.
- **Castopod:** a `Podcast` actor gets both rows; an episode lands in the podcast's community; credits
  match the RSS hosts and guests; a fediverse `href` links to its `User`; a broken feed leaves no
  credits; `Accept` and `Undo` resolve the right row; the byline renders; the API carries
  `extensions.podcast.credits`.
- **Local validation:** seed a PeerTube channel, a Castopod podcast with RSS credits, and a few
  Mastodon/Pixelfed directory entries from fixture files (no network).

### Rollout

One fork migration revising the current head. `daily.sh` gains the refresh. Nothing runs until an
admin runs `flask refresh_discovery` or the daily job fires; pre-load is always a manual admin action.

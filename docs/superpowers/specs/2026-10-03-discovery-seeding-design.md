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
   | Castopod podcasts | index.castopod.org's public export `GET https://index.castopod.org/podcastindex.json` (no auth; a JSON list of every Castopod podcast, about 4 MB): every entry whose `dead` is not 1 and whose `link` is a clean https Castopod actor `https://host/@handle` (also its ActivityPub id), ordered by `popularityScore`; `explicit` 1 is NSFW. Amended 2026-10-05 (owner's decision): replaces the Podcast Index API, which needed an admin key and secret and a per-podcast `socialInteract` check, and found about 7 podcasts | Publishing on Castopod is the opt-in |
   | Mastodon people | Top 20 servers from `https://api.joinmastodon.org/servers`, then each server's `GET /api/v1/directory?local=true&order=active&limit=80` | Only `discoverable` accounts are listed |
   | Pixelfed people | Pixelfed hosts from FediDB `https://api.fedidb.org/v1/servers?software=pixelfed`, then each host's `GET /api/landing/v1/directory` (404 = disabled by the admin; skip the host) | Only `is_suggestable` public accounts are listed |

4. **No credentials:** every source is public, so the admin enters none. Amended 2026-10-05: the Podcast
   Index key and secret fields were removed with that source; settings rows already stored are left as they are.
5. **Castopod authorship:** a podcast actor gets a `User` row (the technical owner) and a `Community` row
   with the same actor URL (`ap_profile_id` is unique per table, not across tables). The Community's
   `ap_id` is `name@host`, lower-case, built as for a remote Group from the podcast's `preferredUsername` and
   domain, never copied from the User (amended 2026-10-05: a podcast found by its handle has a User `ap_id` of
   `@name@host`, which gave the community the url `/c/@name@host`); every resync rebuilds it, which repairs rows
   stored the old way. `Post.user_id`
   stays the podcast actor, and the page always shows the podcast account as the poster, followed by
   the **credits** (amended 2026-10-03 after a security review: credits are claims made by the podcast's
   own feed, so they must never replace or hide the real poster).
6. **Credits:** an ordered list `{name, role, image, profile_url, user_id}` from the podcast's RSS
   `podcast:person` tags: channel-level tags are hosts, item-level tags (`role="guest"`) are guests; the
   item is matched to the episode by link or GUID. A credit links to a fediverse account only when that
   account **vouches back**: its actor document links the podcast (the podcast actor URL or its web URL
   appears in the actor's `url`, `alsoKnownAs`, or a profile-field `attachment`). An unverified credit is
   shown as a plain name, never linked, so a feed cannot attribute an episode to a real person's profile
   without that person's consent.
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
    PeerTube isolation list and names failing `is_bad_name`; tags NSFW; caps entries per host (20, or
    100 for index.castopod.org, whose big hosts list 60-90 podcasts) and per source (500, or 2500 for
    index.castopod.org so its whole index is kept); upserts into `discovery_entry` (unique on `actor_url`); deletes entries not seen for 30
    days.
- **`discovery_entry` table:** `id`, `kind`, `platform`, `actor_url` (unique), `name`, `host`,
  `avatar_url`, `followers`, `nsfw`, `source`, `first_seen`, `last_seen`.
- **Scheduling:** `flask refresh_discovery` CLI command, called from `daily.sh` (the existing daily
  cron). Never runs on a request.
- **Admin page** (where the lemmyverse pre-load lives): a "Pre-load
  channels & podcasts" control (choose N and platforms, preview, then subscribe); attribution text
  naming every data source.
- **Search:** community search (`/communities`) and people search (`/search?search_for=people`) show, on their first page and below the local
  results, the `discovery_entry` rows matching the query that this server does not know yet (no Community or User with
  that actor id). Amended 2026-10-04: the earlier "only when nothing local matches" rule hid the whole directory once
  one result had been opened, because opening it made it local. Results show a platform badge and a join/follow button that resolves the actor through
  `find_actor_or_create`.
- **Castopod mapping:**
  - creating a `Podcast` actor also creates its `Community` row;
  - episode Notes from that actor are filed in its community instead of `microblogs`;
  - the episode audio/cover feature (WP-C) keeps working;
  - credits are parsed from the RSS feed and stored on the post;
  - the post byline shows the podcast account, then "hosted by A, B · with guest C" (verified credits linked, others plain names);
  - every actor lookup that does not pass a type hint (`Accept`, `Undo`, inbox routing,
    `find_actor_by_url` callers) is audited so it resolves the right row for a dual actor.

### Limits, errors and privacy

- **Network:** every fetch uses `get_request` (SSRF guards, redirects off) with a short timeout.
  Per-run budgets: PeerTube at most 5 pages; Mastodon at most 20 servers × 1 directory page of 80;
  Pixelfed at most 20 servers × 2 pages; index.castopod.org one GET of its export, capped at 16 MB (the other
  directories at 5 MB). Calls to the same host are
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
  handling (`discoverable=false`, Pixelfed directory 404), blocklists, budgets and paging, the index.castopod.org
  export (dead and non-Castopod entries skipped, popularity order, its 16 MB cap applying to it alone), and a failing source keeping existing entries.
- **Refresh:** idempotent upsert, 30-day expiry, cleaning of names and URLs.
- **Admin:** no credentials form; pre-load preview honours N and the platform
  filter; subscribe calls the join path once per new community and skips known ones; attribution shown.
- **Search:** local results first, then the directory entries not known here; NSFW hidden by default; join/follow resolves
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

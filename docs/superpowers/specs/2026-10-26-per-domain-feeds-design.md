# Per-domain feeds: design

Date: 2026-10-26. This is follow-up F4 of the onion storefront track (`docs/superpowers/specs/2026-10-17-onion-storefront-design.md` §6).
Users and communities became per-domain in projects C1–C3. Feeds are the one object still domain-blind: a C1 ruling
said "feed names stay globally unique". The owner has now chosen per-domain feeds.

**Terms.**
- The **first domain** is the default domain (SERVER_NAME).
- A **sister domain** is any other clearnet storefront.
- **The onion** is the Tor onion storefront. It is local-only and does not federate.
- A feed's **home domain** is the domain it belongs to.
- An account's **home domain** is the domain it registered on (`User.home_domain()`).

## 1. Intent

Each storefront has its own feeds, the way it has its own communities. A person browsing a domain sees that domain's
feeds, and an account's feeds follow the account's home domain. Feeds from one domain must not leak onto another where
the local-only rules would hide them.

## 2. Goals, non-goals, anti-goals

Goals:
1. **Every local feed has a home domain** (owner). New feeds belong to the creator's home domain, not to the domain they
   are browsing (owner).
2. **Feed views are per domain** (owner).
   - The feed list, menus and sidebar on a domain show that domain's feeds.
   - A logged-in account sees the feeds of its own home domain, whatever storefront it is on. An account from the
     first domain or a sister domain that logs in on the onion sees its home domain's feeds (owner).
   - An onion account sees the onion's feeds. If the onion has no feed to show, it sees the first domain's feeds
     (owner).
   - Logged-out visitors see the feeds of the domain they are on, with the same onion fallback to the first domain's
     feeds (Claude's call, accepted at playback).
   - **Onion feeds render only on the onion** (owner, which settles the conflict with "home domain wherever"). An onion
     account browsing a clearnet domain sees that domain's feeds, as a logged-out visitor would, with the first-domain
     fallback. Its onion feeds are not listed there, and `/f/...` for them answers 404 off the onion.
3. **A feed may contain communities from any domain**, local or remote, as today (owner).
4. **Existing feeds belong to the first domain** (Claude's call). They keep their URLs, `ap_profile_id`, actor documents,
   keys and federation byte-identical.
5. **Names are per domain** (Claude's call):
   - `(home domain, lower(name))` is unique among local feeds. Two domains may each have a feed called `news`.
   - A feed name no longer blocks a user or community name on another domain. The existing same-domain checks between
     feed, user and community names stay as they are.
6. **Links name the domain when it is not the current one** (Claude's call).
   - `/f/name` means this storefront's feed.
   - `/f/name@domain` means another domain's feed, spelled as communities and users already are (`path_name`,
     `stored_path_name`, `link_host`).
6a. **Links in a feed list are spelled relative to the current storefront** (Claude's call). This settles goal 2
    against anti-goal 3. When an account on a storefront other than its home sees its home domain's feeds, each link is
    `path_name(name, feed's domain)`: `/f/name@home` whenever the feed's domain is not the current one. A bare
    `/f/name` always resolves on the current storefront. The resolver never guesses from the viewer's home.
6b. **Another domain's feeds are reachable, not listed** (Claude's call, mirroring communities).
    - `/f/name@sister` opens a non-onion sister feed under its usual `public`/membership rules.
    - Lists, menus and explore show only the current view's feeds (goal 2).
    - Search finds feeds the way it finds communities.
7. **Sister-domain feeds federate from their own domain** (Claude's call).
   - Their actor id, inbox, outbox, followers, following, moderators and public URL are on the home domain.
   - WebFinger `acct:~name@domain` resolves them on that domain only.
   - The first domain's WebFinger no longer answers for another domain's feed. Mirror the community rules
     (`owner_domain`, `owner_base`, `owned_elsewhere`, `federating_clause`).
8. **Onion feeds never federate and are local-only** (Claude's call).
   - Other servers see nothing of them: no actor, no WebFinger, no Announce.
   - Other storefronts and logged-out viewers do not see them. This follows the C2 local-only rules for accounts and
     communities, and the primary rule of the onion unlinkability spec (`2026-10-26-onion-unlinkability-design.md`).

9. **Local-only contents are filtered at display and federation** (Claude's call, under goal 8).
   - A feed may hold any community (goal 3). When it is shown or federated, any community that the C2 local-only rules
     hide from that viewer or server is skipped, and so are its posts: the same `community_local_only` /
     `without_local_only_actors` gates that community lists use.
   - A clearnet feed holding an onion community never shows or announces it off the onion.
   - Adding such a community is allowed only where the adder can see it.
10. **Feed trees and copies stay within one domain** (Claude's call).
    - A child feed has its parent's home domain; a cross-domain `parent_feed_id` is refused at creation and edit (400).
    - `feed_copy` makes a feed on the copier's home domain (goal 1). Copying is allowed only where the source feed is
      visible to the copier.
11. **The API follows the request's storefront** (Claude's call).
    - A bare feed name in the API means the request host's domain. `name@domain` means that domain.
    - `ap_domain` of a local feed holds its home domain's authority (`domain_authority`), as for communities. For
      existing feeds that is the first domain, which is unchanged.
    - The column-level `unique=True` on `Feed.name` is replaced by the per-domain partial index.
12. **Onion feeds get local ids like onion communities** (Claude's call). Their `ap_profile_id` is minted on the
    onion's base URL, so ids stay unique, but no document is ever served for them (goal 8).

Non-goals:
- Moving a feed to another domain. A rename keeps the feed's home domain (Claude's call).
- Per-domain FeedItem rules. Membership stays domain-free (owner: "any domain, same as today").
- Remote feeds. Feeds owned by other servers have no home domain, and how they are fetched and shown does not change.
- Ranking or boosting feeds by domain.

Anti-goals:
- An existing feed's URL, actor document or federation changes. Remote followers must notice nothing.
- An onion feed, or the fact that an account owns one, becomes visible to another domain, a logged-out viewer or
  another server.
- A bare `/f/name` link on one domain silently opens another domain's feed of the same name. This is the namesake
  problem that follow-up F1 fixed for users.
- Invalid client input becomes a 500.

## 3. Constraints

- Mirror how communities became per-domain:
  - the column and backfill (migration `b7e3c1a9d5f2` pattern: `site_domain_id`, a backfill to the default domain,
    and a partial unique index on `(site_domain_id, lower(name)) WHERE ap_id IS NULL`);
  - `instance_id_of(domain)` at creation;
  - `local_names` lookups, `path_name`/`stored_path_name`, `domain_base_url`/`domain_authority`;
  - the `federation_urls` owner helpers;
  - WebFinger;
  - `local_only` clauses.
  Reuse those helpers rather than writing feed-only copies.
- `owner_domain()` in `app/federation_urls.py` returns the default domain for every local feed today. It is the central
  switch.
- Today feed ids are built by hand from SERVER_NAME in `make_feed`, `edit_feed` and `feed_copy`. They move to the
  domain-aware builders.
- Feeds sign with their own key (`feed.private_key`). No per-domain key is needed.
- Project rules:
  - TDD, one commit per task;
  - 100% changed-line and branch coverage (`tests/check_changed_line_coverage.py --branches`), no pragmas;
  - inline imports only if technically unavoidable;
  - slow tests are bugs;
  - invalid input stays a 400/404;
  - no tracked file names the first domain (`tests/test_no_first_domain_literal.py`).
- The API: `get_feed` already accepts `name@ap_domain`. It and the other API feed calls must stay compatible.

## 4. Tests that must exist

- Existing feeds after the migration:
  - they are on the first domain;
  - their actor documents, WebFinger answers and `ap_profile_id` are byte-identical to the documents before the
    migration (use the C3 golden-document fixtures).
- A feed created by a sister-domain account belongs to that domain. Its actor URLs are on that domain, and WebFinger
  answers it there and nowhere else.
- Two domains can each have a feed called `news`. `/f/news` opens each storefront's own feed, and `/f/news@other`
  opens the other one.
- An onion account sees onion feeds, or the first domain's feeds when the onion has none. A first-domain account
  logged in on the onion sees first-domain feeds.
- An onion feed:
  - gets 404 on every other storefront and for logged-out viewers off the onion;
  - has no WebFinger or actor answer anywhere;
  - is never announced.
- A feed name on one domain does not block a user or community name on another domain.
- A feed holds communities from several domains and from remote servers, and shows their posts, as today.

## 5. Left to the builder

- Column and index names. Whether the onion fallback for feed lists lives in one helper or in each list query.
- How the feed menus and `feed_tree_public` filter by domain.
- How many migrations, and in what order the tasks run.

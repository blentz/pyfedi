# Per-domain identity and federation: design

Date: 2026-10-19. Project 3 of `docs/superpowers/specs/2026-10-17-onion-storefront-design.md` (§6). Projects 1–2
(domain registry, host-aware URLs, Tor deploy) and the onion media proxy are live.

## 1. Intent

The first domain will soon add more hell.* domains, and it already serves the onion. Today every domain is a storefront
for the one first-domain identity: an account is a first-domain account wherever it logs in. The owner wants each domain
to be its own instance to the people on it and to the fediverse, while all of them share one server, one database
and one set of site settings.

The onion is the special case. Nobody signs up there. Its main use is people from the other domains logging in
privately as themselves (`wakko@example.org`). Admins may also add accounts that live only on the onion. Those are
local-only: nothing they do leaves this server. And nothing records that anyone used the onion.

## 2. Goals, non-goals, anti-goals

Goals (owner rulings 2026-10-19 unless marked)
- **Each domain is its own identity space.** An account and a community belong to exactly one domain, permanently.
  `wakko@example.org` and `wakko@hell.foo` can be two different people (§6, decided 2026-10-17).
- **Per-domain federation.** A domain with `federates` on publishes its own actors under its own host:
  `https://hell.foo/u/alice`, `!art@hell.foo`, its own WebFinger, NodeInfo, instance actor and shared inbox. To the
  fediverse each domain is a separate instance.
- **Federation-off domains are local-only.** Their accounts' posts, comments, votes and follows never leave this
  server. Their actors are not published (§3.4).
- **Login on any storefront.** A bare username means the account on the domain being logged in on: a bare `wakko`
  on the onion is the onion's wakko, and the first domain's wakko types `wakko@example.org`. A logged-in user acts as their
  home identity whatever storefront they browse.
- **The onion takes no sign-ups.** Admins add accounts to it directly.
- **Each domain browses as its own instance.** It shows everything public, but filtering and ranking treat that
  domain as the local instance: its own content first, then the sister domains' content (local in the database,
  treated as federated), then remote content.
- **Local-only content carries a "Local" badge**, in the style of the live feed's badge
  (`app/templates/_live_bar.html:4`).
- **No onion usage records beyond what exists by ruling.** The gunicorn access log becomes off by default, behind a
  config flag, for debugging only (§3.7).

Non-goals
- Per-domain site settings: blocklists, allowlists, blocked phrases, NSFW policy, default filters. All domains share
  the global settings for now (owner ruling).
- Per-domain admins or moderators. The site's admins administer every domain.
- Per-domain branding (project 4) and log retention beyond the access-log flag (project 5).
- Moving an account or community between domains. Accounts created through the onion during projects 1–2 are
  first-domain accounts and stay so; the server keeps no record of which they are, by design.
- Accounts on other fediverse servers logging in here. Only the domains this server hosts.
- Remote servers learning that the domains share a server. Not hidden, not advertised: they share an IP.

Kept as they are (owner rulings 2026-10-19)
- `last_seen` updates, `read_posts.interacted_at`, the onion-only Redis keys (`onion-user:<id>`,
  `onion_login_fail:acct:*`) and registration-time data (referrer, timezone).
- IP and country are already blank for onion requests (`audit_field`, `get_ip_address`), and IP bans skip them.

Anti-goals (a failure even if it "works")
- **The first domain's identity changes.** Any existing `ap_profile_id`, actor key, `keyId`, WebFinger answer or inbox URL
  that differs after this project. Remote servers would see duplicates or reject signatures.
- **Local-only content leaves the server**, by any path: delivery, an outbox page, a featured collection, a
  relay, a remote fetch of its URL, a reply or vote that references it.
- **A local-only account is discoverable** to another server: WebFinger, actor document, NodeInfo user count.
- **A login reaches the wrong account.** A bare name resolving to another domain's account, or a full handle to a
  same-named account elsewhere.
- **An activity signed by one domain's key on another domain's behalf.**
- **A sister domain sends itself an HTTP request** for something the shared database already holds.

## 3. Design

### 3.1 Ownership
- Each local user and community gets the domain it belongs to (a `site_domain` row). Every existing local row
  belongs to the default domain (the first domain). Remote rows have none. (Claude's call on the shape: a foreign key on
  `user` and `community`.)
- A local name is unique within its domain, case-insensitively, not across domains. Remote rows are unaffected.
  Prod has no local user or community names that differ only in case (checked 2026-10-19), so the index can be
  built without a cleanup step.
- A community belongs to its creator's home domain, whichever storefront they created it on. (Claude's call,
  following "acts as their home identity".)
- A post's or comment's object ID is on its author's home domain, whatever community it is in. A fetch of
  `/post/N` through another of our domains redirects to that ID. (Claude's call: the fediverse's usual rule.)
- Each hosted domain has its own `instance` row. Sister content then reads as federated wherever the code tests the
  instance, and a user can block a sister instance like any other. The default domain keeps row 1.
  (Claude's call.)
- "Local" in the code today means `ap_id IS NULL` or a URL on one of our hosts. Each use is sorted into one of two
  meanings: *hosted here* (any of our domains: storage, moderation, notifications) or *local to the request's
  domain* (feeds, ranking, the Local filter, badges). (Claude's call: the same sorting Project 1 did for URLs.)

### 3.2 Login and registration
- The login form takes `name` or `name@domain`. A bare name looks only in the storefront's domain. A full handle
  looks only in that domain, which must be one this server hosts; otherwise the login fails like a wrong password.
  Email and passkey logins resolve as today (an email is unique site-wide). (Claude's call on email.)
- A logged-in user's pages render on the storefront they are on (Project 1), but they act as their home identity:
  posts, votes and follows are theirs, federated under their home domain if it federates.
- `site_domain.registration` is enforced per domain: anyone, staff+admin, admin-only. The onion row is admin-only,
  so its sign-up page refuses (owner ruling).
- The admin "add user" form gets a domain picker. That is how onion accounts are made.
- Out-of-request messages (email, push) link to the recipient's home domain. Onion accounts' links use the onion
  (§6 of the earlier spec). This replaces ruling I1 of the storefront ledger: third-party mail is no longer forced
  to the default domain. The sender address stays the site's. (Claude's call.)

### 3.3 Per-domain federation
- Actor IDs, inboxes, outboxes, followers collections and object IDs are built from the owning domain, never from
  `SERVER_NAME` or the request host. Existing stored IDs are not rewritten.
- Each federating domain answers WebFinger (`acct:name@domain`, only its own accounts and communities), host-meta,
  NodeInfo (its own counts, software PieFed) and its own instance actor. Each instance actor has its own key pair,
  stored with its domain row. The first domain's instance actor keeps its current key.
- Each domain's shared inbox receives activities for its own actors. One server, one database: an inbound activity
  is stored once however many of our domains' actors it addresses.
- Outbound: an activity is signed with its actor's key and its `keyId` is the actor's own ID, on the actor's domain.
  Instance-level activities (relay subscriptions, instance-actor fetches) use the domain's instance actor.
- **Between sister domains, no HTTP.** A hell.foo user commenting on a first-domain post is stored directly. Remote
  followers of either side get the activity from the originating domain's actor, exactly as if the sister had
  received it over the network. (Owner: "locally pseudo-federated".)
- Relays stay subscribed by the default domain only. Relayed content lands in the shared database, so every domain
  sees it. (Claude's call.)
- Caddy: one site block per federating domain with automatic TLS, using the same `piefed_routes` snippet. DNS and
  the domain row are the owner's steps.

### 3.4 Local-only domains (federates off)
- Their accounts' posts and comments stay on this server, in any community, including one on the first domain or a remote one.
  Local users see them; no other server does.
- **Local-only is stamped when the content is made**, on each post, comment, vote and follow, not worked out later
  from `site_domain.federates`. Everything posted in a local-only community is local-only, whoever posts it.
  (Claude's call.)
- **The federation switch locks** once a domain has accounts or communities: the admin form refuses to change
  `federates`, so nothing held back is ever published by surprise and no remote follower is stranded. (Owner ruling
  2026-10-19.)
- **Only logged-in users see local-only content.** Anonymous web pages, RSS, sitemaps, link previews (OpenGraph,
  oEmbed) and anonymous API calls leave it out, so it stays on this server against scrapers too. The first domain is a
  private instance today, so this bites only when a domain is public. (Owner ruling 2026-10-19.)
- A local-only account never appears in federated output: it cannot own or moderate a federating community, a
  mention of it goes out as plain text without the actor link, and federated member counts leave it out.
  (Claude's call.)
- A local-only account may join a remote community, but new content arrives only while a federating member also
  follows it. The last federating member leaving sends the usual Undo Follow. (Claude's call.)
- A reply to local-only content is local-only too, and so is a vote or boost on it. A local-only account's votes and
  boosts are never sent. (Owner confirmed 2026-10-19.)
- A local-only account can join communities here, including remote ones whose content already arrives through other
  members, but no Follow is sent. It cannot follow remote people or message them.
- Its profile, WebFinger and actor document answer 404 to other servers. NodeInfo does not count it.
- A community created on a local-only domain is local-only by the same rule.
- Local-only posts and comments carry the "Local" badge wherever they are listed or shown. (Claude's call: a colour
  distinct from Live's green.)

### 3.5 Browsing as the domain's instance
- The Local feed and every "local" filter mean the request's domain.
- Ranking (the home sorts, community lists, search) prefers, in order: the request's domain, the sister domains,
  remote. (Owner ruling; the weighting is left to the builder.)
- Everything public stays visible on every domain. Settings are global (§2 non-goals).

### 3.6 Existing data and deploy
- One migration adds the ownership columns and backfills every local user and community to the default domain,
  plus per-domain unique indexes. The first domain's rows, IDs and keys are unchanged.
- Adding a hell.* domain is an admin row plus a Caddy site block plus DNS. The deploy guide gets the steps.

### 3.7 Access logging (owner ruling 2026-10-19)
- The gunicorn access log is off unless a config flag turns it on (Claude's call: an environment variable in
  `.env.docker`, read by `gunicorn.conf.py`; default off). It is for debugging only.
- fail2ban's register and scan jails read gunicorn access lines today. Instead the app logs only the events they
  need, in the style of the existing `auth: login failed ip=<ip>` line: a registration attempt, and a request to a
  probe path (`/wp-`, `/.env`, `/phpmyadmin`, `/cgi-bin`, `/xmlrpc.php`) answered 4xx. Onion requests log
  `ip=onion`, which no jail matches. The jail filters in `deploy/fail2ban/` change to match. (Owner chose this.)
- The relay-delivery diagnosis that counted per-relay access lines now needs the flag turned on.

## 4. Constraints
- TDD, one commit per logical change, 100% changed-line and branch coverage, gates run from repo tools, slow tests
  are bugs. Inline imports only if technically unavoidable.
- Fuzz harness contract: no DB, no network in campaigns. Any fuzz target reaching the per-domain resolution stubs it.
- Federation regressions are tested against the first domain's real stored shapes: an existing actor's ID, key and
  WebFinger answer are byte-identical before and after.
- Deploy: owner pushes; the migration runs through autodeploy (age-encrypted dump first). Caddyfile is tracked;
  fail2ban, env and quadlet changes are local-only `deploy/` sources.

## 5. Left to the builder
- How the ownership resolution is cached per request, and the exact column and index names.
- The ranking weights between own, sister and remote content.
- The order of work. A sensible split is: (a) ownership, login and registration, the onion local-only rules and the
  badge, with the first domain as the only federating domain; (b) per-domain federation for a second domain; (c) the
  access-log flag and the fail2ban lines, which stand alone and can go first.
- Test layout. Tests must include: a bare and a full-handle login on two domains with the same name; a local-only
  account's post in a remote community producing no outbound delivery; WebFinger and actor 404s for a local-only
  account; the first domain's actor document and WebFinger byte-identical; a sister-domain reply stored without HTTP and
  delivered to a remote follower signed by the right domain.

# Onion storefront: multi-domain PieFed and a Tor onion service for the first domain

Date: 2026-10-17. Status: design, awaiting owner approval.

## 1. Intent

The first domain should be reachable on the Tor network as well as the clearnet, for readers and posters who
want privacy. A visitor who arrives on the onion address must stay on it: every link, redirect, image,
form and stream they meet uses the onion host.

The owner's longer aim is wider than one onion. PieFed becomes **one aggregate community behind several
uniquely branded storefronts**. Each storefront is a domain with its own users and identity
(`alice@<onion>` is a different person from `alice@example.org`, permanently). All storefronts share one
database, the same communities, posts, local feed and moderation, without the cost of separate instances.

This document designs the first two of five projects in full and records what is already decided about
the other three.

| # | Project | This document |
|---|---------|---------------|
| 1 | Domain registry + host-aware URLs | full design |
| 2 | Tor deploy for the first domain | full design |
| 3 | Per-domain identity (multitenancy) | decided rulings only |
| 4 | Per-domain branding | decided rulings only |
| 5 | Logging minimisation across the deploy | decided rulings only |

Projects 1 and 2 together deliver a working onion storefront for existing first-domain users. Accounts
created through the onion during that window are ordinary first-domain accounts. See §6.

## 2. Goals, non-goals, anti-goals

Goals
- The site answers on every domain the admin registers. The storefront is chosen per request.
- A visitor never meets a URL for a different storefront than the one they are on.
- The first domain keeps federating exactly as today. Its ActivityPub identity does not change.
- The onion is fast, survives restarts and redeploys, and keeps the same address forever.
- Abuse arriving over Tor cannot lock honest onion users out.

Non-goals (for projects 1–2)
- Per-domain accounts, communities, federation or registration policy: that is project 3. The columns
  exist in project 1 but are not enforced.
- Per-domain branding: project 4.
- Reducing what the deploy logs: project 5. Projects 1–2 add the logging that fail2ban needs, and
  project 5 decides retention.
- Hiding where the first-domain server is. Its IP is public on the clearnet.

Anti-goals (a failure even if everything "works")
- An onion page that loads anything from `https://example.org` (links, images, scripts, redirects,
  `<link rel=canonical>`, OpenGraph tags, SSE). That breaks the privacy promise.
- An ActivityPub ID, signature `keyId` or WebFinger answer that changes because of the host a request
  arrived on. Remote servers would see duplicate actors or reject signatures.
- One abusive Tor user getting every onion user banned, rate-limited or locked out. All of them share
  one apparent IP.
- Losing or leaking the onion private key.

## 3. Project 1: domain registry and host-aware URLs

### 3.1 Data model

New table `site_domain`, one row per domain the site answers on:

| column | type | meaning |
|---|---|---|
| `id` | int PK | |
| `domain` | varchar(255), unique, lower-case | host name only, no scheme or port |
| `scheme` | `'https'` / `'http'` | used to build URLs for this storefront |
| `federates` | bool | project 3. Stored, not enforced in project 1 |
| `registration` | enum `anyone` / `staff` / `admin` | project 3. Stored, not enforced in project 1 |
| `is_default` | bool | exactly one row true |
| `is_onion` | bool | derived: `domain` ends in `.onion`. Stored to make queries simple |

Rules
- Exactly one default. Enforce it in the DB with a partial unique index on `is_default` where true, and
  in the admin form with a radio group. The default cannot be deleted.
- The migration seeds one row from `SERVER_NAME`: `scheme` comes from `HTTP_PROTOCOL` (`https`, or
  `https` for `mixed`; see §3.6), `federates=true`, `registration` mapped from the current site
  registration mode, `is_default=true`.
- `SERVER_NAME` in the environment stays the **canonical federation domain** for projects 1–2. Startup
  checks that it matches the default row and logs an error if it does not. Project 3 revisits this.
- Until project 3, the default row is locked to the `SERVER_NAME` domain. The admin UI shows the radio
  but disables it, with a note, so background emails cannot switch to onion links. (Claude's call.)
- Changes to the table take effect without a restart: cache it in Redis next to the other site
  settings, and clear the cache on save.

### 3.2 Admin UI

A new "Domains" section in admin site settings (`app/admin/`). It is a table with one row per domain:
domain, scheme, federate checkbox, registration dropdown (Anyone / Staff and admins / Admins only), and a
"Default" radio button. Rows can be added and removed. Project 1 labels the federate and registration
controls as "takes effect in a later release". Adding a domain does not configure DNS, TLS or Tor. The
form says so and links to `deploy/DEPLOY_GUIDE.md`.

### 3.3 Resolving the storefront per request

A `before_request` hook resolves `g.storefront` (a `site_domain` row) from `request.host`, without the
port and lower-cased.
- A known host gets that row.
- An unknown host gets HTTP 421 Misdirected Request with a plain body, and no redirect to the default
  domain. (Builder's call to confirm: health checks that hit the container by IP or `web:5000` need an
  exemption. Find them in the quadlets and autodeploy health URL.)
- Outside a request (celery tasks, CLI, emails, push), there is no storefront. Code uses the default row.

Two helpers replace direct reads of the config for **presentation** URLs:
- `storefront_url()`: `f"{scheme}://{domain}"` of `g.storefront`, falling back to the default row.
- `canonical_url()`: the federation base, today's `SERVER_URL`.

`url_for(..., _external=True)` must produce the storefront host. Flask derives it from the request
already, but `SERVER_NAME` set in the Flask config pins it. The builder checks how `app.config
['SERVER_NAME']` interacts with Flask routing (Flask uses it for host matching and `url_for` outside
requests) and makes requests on any registered host route normally.

### 3.4 Sorting the URL call sites

There are about 316 uses of `SERVER_URL` and 185 of `SERVER_NAME` in `app/**/*.py`, plus 33
`_external=True`. Each one goes into one of two classes.

- **Federation / identity → `canonical_url()` / canonical name.** ActivityPub object and actor IDs,
  `inbox`/`outbox`/`followers` URLs, HTTP-signature `keyId`, WebFinger, NodeInfo, `ap_profile_id`
  generation, outbound activity bodies, anything stored in the DB as an identifier, and comparisons that
  decide whether a URL is local. Local-ness checks must accept **every** registered domain, so that a
  pasted onion link to a local post resolves as local.
- **Presentation → `storefront_url()`.** HTML pages, redirects, media URLs (`File.view_url` and
  friends, `app/models.py:1037`), RSS/Atom, OpenGraph and `canonical` tags, the manifest, emails sent
  during a request, the JS config that the notification and live streams use, OAuth/API responses
  consumed by browsers.

Grey areas, decided:
- **ActivityPub JSON served to a browser or client on the onion** (e.g. `Accept: application/activity+json`
  sent to the onion host) uses canonical IDs. Identity never varies by host.
- **The API used by mobile apps** uses `storefront_url()` for links it returns for display, and
  canonical IDs for identifiers.
- **Emails from background jobs** use the default storefront until project 3, which switches them to the
  recipient's home domain.

The work is mechanical but large. The builder should sort by module, keep a checklist in the plan,
and add a test (below) that fails on any unsorted direct use of `SERVER_URL`/`SERVER_NAME` outside an
allow-list of the canonical helpers.

### 3.4a Cache keys

The app has 107 `@cache.cached` / `@cache.memoize` uses. Flask-Caching's default view key is the request
path only, so a page cached on the first domain would be served on the onion. Every presentation-cache key gains
the storefront host, e.g. through a shared `key_prefix` / `make_cache_key` that adds `g.storefront.domain`.
Memoized functions that return presentation URLs either take the host as an argument or return
host-relative values. A test renders the same cached page on two hosts and checks each gets its own host.

### 3.5 Rewriting stored content

Post, comment, sidebar and profile HTML is stored rendered (`body_html`) and contains absolute links to
`https://example.org/...` that users pasted. At render time, a Jinja filter rewrites `href`/`src`
attributes whose host is **any registered domain** to the current storefront's scheme and host. It
rewrites attributes only, never text. It runs on the already-sanitised HTML, so it adds no new markup
paths. (Owner's intent: onion visitors stay on the onion. The approach is Claude's call.)

The filter must be cheap. Skip the parse when the HTML holds no registered domain string.

**On save**, links whose host is any registered domain are rewritten to the canonical host before
`body_html` is stored. This covers posts, comments, profiles, sidebars, and community and site
descriptions. Stored and federated content therefore never carries onion links. Render-time rewriting
then maps them to the visitor's host. Existing rows already hold canonical links, so no data migration is
needed. (Claude's call.)

### 3.6 Cookies, sessions, CSRF

- Browsers scope cookies by host, so each storefront has its own login session. Any user can log in on
  any storefront (owner ruling). Project 3 adds handle-qualified login.
- `SESSION_COOKIE_DOMAIN` must stay unset (host-only cookies). The builder verifies it is not set.
- `SESSION_COOKIE_SECURE=1` over plain-http onion: Tor Browser treats `.onion` as a secure context.
  **Spike first**: confirm in current Tor Browser that a `Secure` cookie set over `http://<onion>` is
  stored and sent back. If it is not, set the `Secure` flag per request (true when
  `g.storefront.scheme == 'https'` or `is_onion`) rather than globally.
- `HTTP_PROTOCOL=mixed` (an upstream feature) is not used on the first domain. Per-domain `scheme` replaces
  it for this purpose. Keep `mixed` working for upstream users: it maps to the default row's scheme for
  presentation and https for federation, as it does today.
- CSRF tokens are session-bound, so they work unchanged.

### 3.7 Client IP for onion requests

`get_ip_address()` (`app/__init__.py:59`) feeds rate limits, IP bans, the honeypot ban, geolocation and
the `user.ip_address` column. Over the onion, the "client IP" is the Tor container's address on
`pyfedi.network`, shared by every onion visitor.

- **Marking onion traffic.** Caddy's internal onion listener (§4.3) sets `X-PieFed-Via: onion` and the
  public listeners strip any client-sent copy. The app treats a request as onion only when that header is
  present **and** the TCP peer is Caddy. Host alone is client-writable and is not trusted for this.
  - `ProxyFix(x_for=1)` has already replaced `request.remote_addr` with the last `X-Forwarded-For` entry,
    which for onion traffic is the Tor container. Check the original peer instead:
    `request.environ['werkzeug.proxy_fix.orig']['REMOTE_ADDR']`.
  - `pyfedi.network` gets a pinned subnet (`Subnet=` in the `.network` quadlet), and `pyfedi-caddy` gets a
    static `IP=`. The trusted Caddy address comes from a config variable, `ONION_PROXY_IP`. The fail2ban
    `ignoreip` (§4.5) uses the same subnet. (Claude's call.)
  - When `ONION_PROXY_IP` is unset, no request is treated as onion. Startup logs a warning when an onion
    domain is registered while it is unset.
- **For onion requests:**
  - IP bans and the honeypot ban are skipped. A honeypot hit ends the session instead.
  - Geolocation is skipped.
  - `user.ip_address` and other IP audit fields store nothing (`NULL`), never the container IP.
  - The rate-limit key is per session for anonymous routes. Registration is limited per session. Tor
    proof-of-work (§4.4) makes cycling sessions expensive. Owner ruling: per-session limits plus Tor PoW.
    Requests with no session cookie: see §3.7a.
  - Login also has a per-**target-account** limit on onion attempts only. Its counter is separate from
    clearnet, so an onion attacker cannot lock Alice out on the first domain. Tripping it adds a growing delay
    (doubling, capped), never a hard block. It covers web, API and mobile-app login. (Claude's call.)
- Clearnet behaviour is unchanged.

### 3.7a Onion requests with no session (owner ruling: options A and C)

A Tor client can drop cookies on every request, which defeats per-session limits. Two defences apply to
onion requests only. Clearnet is unchanged.

- **C. Session first.** A cookieless request to a rate-limited route gets a `303` to the same URL that
  sets the session cookie. A cookieless `POST` gets the form back, with a new session and a "please try
  again" message. A cookieless `POST` cannot be valid anyway: its CSRF token is bound to a session. This
  stops lazy bots. A determined attacker can fetch a new cookie each time, so A is what costs them.
- **A. Proof-of-work on login and registration.** The form carries a server-issued challenge: an HMAC-
  signed salt, a difficulty and an expiry. The browser finds a nonce whose hash meets the difficulty
  before submitting. The server checks the signature, the expiry and the solution, and rejects a reused
  challenge, tracked in Redis until it expires.
  - Target a few hundred ms on a mid-range laptop in Tor Browser at "Standard". Difficulty is an admin
    setting with that default.
  - All JS and any library are self-hosted under `/static`. No third-party fetch (§2 anti-goal). An
    existing MIT library such as ALTCHA is fine if it is vendored. Builder's call.
  - **No-JS fallback** (Tor Browser "Safest" disables JS). A `<noscript>` link goes to a wait step: the
    server issues a signed token that only becomes valid after N seconds (default 15), and the page
    refreshes itself with `<meta http-equiv=refresh>` into the form. This costs a scripted attacker wall
    time per attempt, though not CPU, and they can run many in parallel. So the fallback is weaker than
    the hash puzzle, and the per-session and per-account limits still apply on top. (Claude's call.)
  - It covers web login and registration. API and mobile-app login over the onion get the per-account
    limit (§3.7) and C, but no puzzle, since apps cannot run it. Builder records this gap.

### 3.8 Tests (TDD, per `defect-fix-workflow` conventions)

- Model: one default enforced. Seed migration from env. Unknown host gets 421.
- A request on host B renders a page whose every `href`/`src`/`action`/`content` URL is on host B. Use
  a crawler-style test over the main pages (index, community, post, user, login, register, settings,
  search), with fixtures that hold content linking to host A.
- ActivityPub actor/object JSON fetched via host B contains only canonical IDs.
- `url_for(_external=True)` follows the request host. Outside a request it gives the default.
- Stored-content filter: rewrites registered hosts, leaves others and text alone, and skips the parse on
  no match.
- Onion marker: honoured only from Caddy's address with the header. Ignored when a client sends it. IP
  ban, honeypot and audit behaviour as in §3.7. Rate-limit key per session / per target account.
- Onion session-first redirect. PoW challenge: valid, expired, wrong signature, reused, and unsolved
  cases. No-JS wait token rejected before N seconds and accepted after. Clearnet login never asks for
  either.
- Lint-style test: no direct `SERVER_URL`/`SERVER_NAME` reads outside the allow-list.

## 4. Project 2: Tor deploy for the first domain

Deploy sources live in the local-only `deploy/quadlet/` and `deploy/systemd/` (git-ignored, see
`first-domain-quadlet-deploy` memory). Units are installed over ssh as `rocky@example.org`. Back up the unit
files and the Caddyfile to `~/backups` before editing.

### 4.1 Tor container

- `pyfedi-tor.container` runs a locally built image `localhost/pyfedi-tor`, from a small distro base
  with the distro's `tor` package. Pin the base by digest and record the tor version. No third-party
  Docker Hub tor images (Claude's call).
- Its `torrc`:
  - `HiddenServiceDir /var/lib/tor/hs_pyfedi`, `HiddenServiceVersion 3`
  - `HiddenServicePort 80 pyfedi_caddy:8081` (internal onion listener, §4.3)
  - `HiddenServiceNonAnonymous 1` and `HiddenServiceSingleHopMode 1`, `SocksPort 0`: single-onion
    mode. The server location is already public, and this roughly halves circuit length for users.
    Users stay anonymous. (Claude's call.)
  - PoW and DoS defences, §4.4.
  - `Log notice stdout` to journald for the fail2ban jail, §4.5.
- `Network=pyfedi.network`. No published ports. It needs outbound internet only.
- A plain (non-vanity) address. Builder's note: generating a vanity prefix is a separate optional step
  with `mkp224o`, if the owner later wants one.

### 4.2 Onion key storage and backup

- Volume `pyfedi-tor-data.volume` holds `hs_pyfedi/`. It must persist: losing
  `hs_ed25519_secret_key` loses the address for good.
- Back up `hs_ed25519_secret_key` once, age-encrypted to the same recipient as the DB dumps (the owner
  holds `~/first-domain-restore.key`), to `~/backups/tor/`. Never leave a plaintext copy outside the
  volume.
- The onion hostname goes into the `site_domain` table through the admin UI (project 1). It is also
  recorded in `deploy/DEPLOY_GUIDE.md`.

### 4.3 Caddy

- A new site block `http://<onion>:8081` (internal only, not in `PublishPort`) with the same handlers
  as `example.org`: SSE `handle` blocks, `request_body max_size 510MiB`, the `lb_try_duration` retry,
  `encode`. Factor the shared handlers into a Caddy snippet `(piefed_routes)` imported by both blocks,
  so they cannot drift.
- The onion block sets `request_header X-PieFed-Via onion`. The `example.org` block does
  `request_header -X-PieFed-Via` (strip).
- The `example.org` block adds `header Onion-Location http://<onion>{uri}`.
- Caddy must not try to get a certificate for the onion. A `http://` site address avoids ACME.
- `pyfedi-caddy.container`: no new published ports. Tor reaches 8081 over `pyfedi.network`.

### 4.4 Tor abuse defences

- `HiddenServicePoWDefensesEnabled 1`. Tor raises the puzzle effort under load by itself. Leave the
  queue settings at their defaults at first, and record them.
- `HiddenServiceEnableIntroDoSDefense 1` with tor's default rate and burst.
- Builder checks the distro tor build includes PoW (it needs tor ≥ 0.4.8 built with `--enable-gpl`).
  If it does not, build tor from source in the image.

### 4.5 fail2ban (owner ruling: a Tor log jail plus clearnet web jails)

Prod runs fail2ban with only an `sshd` jail.

- **Tor jail**, `pyfedi-tor`. It reads tor's journald output for signs of attack on the service
  (intro-point DoS defences triggering, PoW effort spiking, repeated circuit failures). Onion visitors
  have no IP to ban, so its action is **alert, not ban**: email the owner via the autodeploy
  `notify.py`, at most once an hour. The builder picks the log patterns from tor's real messages and
  records them.
- **Web jails**, reading the `pyfedi-web` journald (gunicorn access log already carries the real client
  IP via `ProxyFix`):
  - `pyfedi-login`: failed logins. The app gains a log line on login failure, `auth: login failed
    ip=<ip>` (no username, to limit what is logged). Ban after 10 in 10 min for 1 h. Builder's call on
    exact thresholds.
  - `pyfedi-register`: registration attempts at a high rate from one IP.
  - `pyfedi-scan`: 404/400 floods on common scanner paths (`/wp-`, `/.env`, `/phpmyadmin`, …).
- `ignoreip` must include the `pyfedi.network` subnet, so the Tor container, Caddy and internal
  services are never banned.
- **Ban action must work with rootful podman.** Published ports are DNATed by netavark, so a firewalld
  rule on the host INPUT chain does not see the traffic. The builder verifies with a real test ban that
  the chosen action blocks traffic to 80/443, e.g. by matching in the forward path or using nftables in
  netavark's chain.
- ActivityPub `POST /inbox` traffic from remote servers must never trip a web jail. The patterns
  exclude `/inbox`, and the builder checks against a day of real logs before enabling.

### 4.6 Rollout and verification

1. Ship project 1 (app) through the normal autodeploy. Verify clearnet behaviour is unchanged.
2. Build and start `pyfedi-tor`. Read the hostname, back up the key, add the domain row in admin.
3. Update the Caddyfile and restart Caddy.
4. From Tor Browser: browse, log in, post, upload an image, receive a live notification. Check the
   network panel shows no request to `example.org`.
5. Enable the fail2ban jails. Run a test ban from an outside IP and confirm it blocks 443.
6. Update `deploy/DEPLOY_GUIDE.md`: the onion address, the backup location, how to restore the key.

## 5. Constraints

- The prod host has 7.6 GB RAM, about 2 GB free, and no swap. tor uses roughly 50–100 MB, which is fine.
- Deploys are automatic from the deploy branch on GitHub. Pushing is the owner's job.
- pgdata is on LUKS. A reboot needs the owner to unlock it, and that is unchanged.
- Inline imports only if technically unavoidable (owner rule).
- TDD and one commit per logical change (owner workflow).

## 6. Decided for later projects

**Project 3, per-domain identity**
- An account belongs to exactly one domain, permanently. Each domain is a separate identity space.
  Communities belong to a domain too.
- Domains with `federates` publish ActivityPub actors under their own host. An onion-federated actor is
  only reachable by Tor-capable servers.
- `registration` is enforced per domain: anyone / staff+admin / admin-only.
- Any user can log in on any storefront. A bare username resolves to the storefront's own domain.
  Other domains' users type the full handle (`bob@example.org`). Claude's call.
- Out-of-request content (emails, push) uses the recipient's home domain.
- Open: what federation does for a domain with `federates` off. Its users could be local-only, or
  federate under the default domain.
- Accounts created through the onion during projects 1–2 are first-domain accounts. Project 3 must decide
  whether to offer them a move.

**Project 4, branding**: per-domain site name, logo, colours, custom CSS and sidebar.

**Project 5, logging minimisation**: across the whole deploy, once the onion is known to work. It has to
balance against the fail2ban jails in §4.5, which need short-lived logs with IPs.

## 7. Left to the builder

The order the URL call sites are sorted in. Exact fail2ban thresholds and patterns. Caddy snippet
structure. Image base. The Flask `SERVER_NAME` routing mechanics. The PoW queue tuning. Anything marked
"builder's call" above.

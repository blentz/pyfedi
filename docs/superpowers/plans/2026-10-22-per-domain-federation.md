# Per-domain federation (plan C3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A second federating domain is a complete, separate ActivityPub server to the fediverse: its actors, objects, activities, keys, discovery documents, inbox and media all live on its own domain, and nothing it federates names the first domain.

**Architecture:** Every federated URL is built from its *owner's* domain, never from `SERVER_URL`/`SERVER_NAME` or the request host. One module, `app/federation_urls.py`, owns the rules: `owner_domain(obj)` (user/community → its `site_domain_id`; post/reply/PM/vote → its author's; feed → the default), `owner_base(obj)`, `activity_id(verb, owner)`, and the instance actor's URL and key per domain. The first domain's outputs come out byte-identical because the default domain's base is `SERVER_URL` exactly (C1's `domain_base_url`). A source-scan gate forbids new `SERVER_URL`/`SERVER_NAME` in federated-id construction.

**Tech Stack:** Flask, SQLAlchemy/Postgres, Alembic, Celery, cryptography (RSA keys), pytest (`./run_tests.sh`).

**Spec:** docs/superpowers/specs/2026-10-19-per-domain-identity-design.md §3.3 (and §3.1 object IDs). C3 recon is quoted in the ledger's pre-flight section.

## Global Constraints

- Owner ruling 2026-10-22 ("Own domain only"): everything a sister domain's actors and content send or publish uses its own domain — actor and object ids, activity ids, inbox/sharedInbox, keys and keyId, media URLs, User-Agent, site info (NodeInfo, `/api/v1/instance`, `/api/v3/site`). Nothing it federates names the first domain.
- Owner ruling 2026-10-22 ("Ship dark"): no live second domain yet. The Domains form starts accepting `federates=True` for a non-default domain; live checks happen when the owner adds one.
- The first domain byte-identical: its actor, instance actor (`/actor`, `/`), WebFinger, host-meta, community, user and feed documents, and the shape of every id it emits are unchanged; NodeInfo may differ only in counts. Existing stored ids are never rewritten.
- A post's or comment's object id is on its author's home domain, whatever community it is in (spec §3.1); a fetch of `/post/N` or `/comment/N` through another of our domains redirects to that id.
- The default domain's instance actor keeps `Site` row 1's key pair; every other federating domain gets its own pair, made when the row is created (Task 1 migration backfills existing rows).
- Feeds stay on the default domain (global names, ruled in C1).
- Relays stay subscribed by the default domain only (spec, Claude's call).
- Local-only domains (onion) keep every C1/C2 rule: no WebFinger, actor or NodeInfo answer to other servers; C2 hiding unchanged. Nothing in C3 may publish a local-only domain's hostname.
- No-request paths (Celery, CLI) build ids from the owner, never from `current_storefront()`.
- TDD, one commit per task or defect, 100% changed-line and branch coverage (`python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`), inline imports only if technically unavoidable, slow tests are bugs; fuzz targets reaching new helpers stub them.

## Review Focus

1. A first-domain user replies in a shop.example community: the reply id is on the first domain, signed by the user's first-domain key; the community's Announce is on shop.example, signed by the community's key, with a shop.example activity id.
2. A Celery task sends a shop.example user's activity: activity id, keyId, User-Agent and any embedded URLs are all shop.example.
3. Two requests for `/.well-known/nodeinfo`, `/nodeinfo/2.1`, `/.well-known/host-meta` and a WebFinger resource on the first domain and on shop.example in a row: each host gets its own body (no cache bleed).
4. A remote server fetches `https://shop.example/post/N` for a first-domain user's post: 302 to the first-domain id; and `https://example.org/post/M` for a shop.example post: 302 to shop.example.
5. An inbound activity whose target is a shop.example object is matched (the "is it ours" tests accept every federating hosted domain), stored once, and a Create for a first-domain post sent to both domains' inboxes is processed once.

---

### Task 1: Each federating domain has its own instance actor key

**Files:**
- Create: `migrations/versions/<rev>_site_domain_keys.py` (down_revision `c8f4d2a6b9e1`): `site_domain.public_key`, `site_domain.private_key` (Text, nullable); backfill a fresh RSA pair for every non-default row; the default row stays NULL (it reads `Site` row 1).
- Modify: `app/models.py` SiteDomain (columns; the C2 `before_insert` hook also generates a pair for a non-default row), `app/storefront.py` DomainInfo (no key in the cached tuple — keys are read on demand).
- Create: `app/federation_urls.py` with `owner_domain(obj) -> DomainInfo`, `owner_base(obj) -> str`, `domain_of_url(url) -> DomainInfo | None`, `activity_id(verb: str, owner, token: str | None = None) -> str`, `instance_actor_url(domain) -> str`, `instance_actor_signing(domain) -> tuple[str, str]` (private key, keyId).
- Test: `tests/test_federation_urls.py`.

**Interfaces:** Produces the helpers above. For the default domain every helper returns exactly today's string (`SERVER_URL + '/actor'`, `SERVER_URL + '/actor#main-key'`, `SERVER_URL + '/activities/<verb>/<token>'`) and `Site` row 1's key.

- [ ] Tests: default-domain helpers equal today's literals; a shop.example user/community/post/reply resolves to shop.example; a feed resolves to the default; a new non-default SiteDomain gets a key pair, the default does not; the migration backfills keys for existing non-default rows and is idempotent; `instance_actor_signing(shop)` signs with shop's key and a `https://shop.example/actor#main-key` keyId; no-request call resolves by owner.
- [ ] Commit: `feat: each federating domain has its own instance actor key (C3)`.

### Task 2: Actor and collection documents name their own domain

**Files:** `app/activitypub/util.py:1087-1155` (`community_actor_data` hard-codes `https://{SERVER_NAME}/c/...` and sharedInbox), `app/activitypub/routes.py:545-578` (user doc `sharedInbox`, `attributionDomains`, icon/image URLs), `:635` `community_collection_id`, `:3773-3834` and `:3958/:3994` (feed docs stay default — verify, allowlist), `app/shared/tasks/groups.py:65-109` (Group Update), `app/main/routes.py:1294-1362` (`/` Application, `/actor`, `/actor/outbox` answer for the request's domain: id, key, name; the default unchanged); model fallbacks `app/models.py:1554/1558, 2313/2317, 2161, 2504` (owner's base); creation paths still on SERVER_URL: `app/cli.py:298-300, 1259-1261`, `app/dev/routes.py:49-52`, `app/admin/routes.py:1661-1667, 2697-2702` (owner's domain; feeds default).
- Tests: for a shop.example user and community, every URL in the actor document (id, inbox, outbox, followers, following, featured, moderators, sharedInbox, publicKey id/owner, icon, image, url, attributionDomains) is on shop.example; the first domain's documents compare byte-for-byte to a stored golden copy (add golden-copy tests for `/u/<user>`, `/c/<community>`, `/actor`, `/` — capture them from the current code first, before any change); `/actor` on shop.example has shop's key.
- Commit: `feat: an account's or community's ActivityPub document names its own domain (C3)`.

### Task 3: Object and activity ids live on the owner's domain

**Files:** recon §1d: `Post.generate_ap_id` (`app/models.py:3795-3810`, both the friendly and fallback forms), `Post.profile_id` (`:3842`), `PostReply.new`/`profile_id` (`:4388, :4520`), serializers (`app/activitypub/util.py:393-558`, `serializers/peertube.py:66`, `serializers/microblog.py:44,128`), PM ids (`app/chat/util.py:24`), quote authorisation (`app/activitypub/routes.py:4102-4104`, `util.py:5909-5912`, `app/quotes.py:36`), flair tag ids (`app/models.py:1682, 3917, 5855, 3033`), and every `SERVER_URL/activities/...` builder in recon §1d's activity table (≈90 sites) → `activity_id(verb, owner)` where owner is the activity's actor. `/activities/<type>/<id>` (`routes.py:3050`) looks up the id on the request's domain. `/post/<id>`, `/comment/<id>` and `/private_message/<id>` fetched with an AP Accept through a domain that is not the object's owner → 302 to the owner's id. `domains_sent_to = [SERVER_NAME]` (`shared/tasks/notes.py:189`, `pages.py:379`) → all hosted domains.
- Create the gate `tests/test_federated_id_literals.py` (model: tests/test_instance_id_literals.py): AST scan of app/ flagging `SERVER_URL`/`SERVER_NAME`/`config['SERVER_NAME']` used to build a string that is then assigned to an `ap_*`/`id`/`url` key or passed to a signing/send call; allowlist with reasons (`rendered link`, `default-only by ruling: relays|feeds`, `User-Agent: Task 5`, `media: Task 6`, `discovery: Task 4`), stale-entry test. Task 4-6 entries are removed by those tasks.
- Tests: hell user's reply in a shop community → reply id on hell; shop user's post → shop id; Announce/Like/Follow/Undo/Delete/Update/Accept ids on the actor's domain, from a Celery-style call with no request; hell ids unchanged in shape (golden strings); the 302 redirects both ways; a remote `Like` of a shop post is matched (Task 5 dependency: use the is-it-ours helper Task 5 creates only if already present, else allowlist and note).
- Commit: `feat: posts, comments and activities get ids on their owner's domain (C3)`.

### Task 4: Discovery answers per domain

**Files:** `app/activitypub/routes.py:145-280` (WebFinger: drop the C1/C2 default-only check at `:210-214`; subject `acct:{name}@{row's domain}`; only rows on the request's own domain — a shop.example resource asked of the first domain 404s, and vice versa; the instance-actor JRD per domain; local-only domains still 404; memo key includes the request host), `:280-378` (`/.well-known/nodeinfo`, `/nodeinfo/2.0`, `/2.1`: hrefs and counts for the request's domain — users, posts, comments on that domain's instance; caches keyed by host), `:291-310` host-meta (per host, cached per host), `:381` `/api/v1/instance` (`uri`), `:449` `/api/v3/site` and `app/activitypub/util.py:6196-6201`, `app/api/alpha/views.py:1423` (site `actor_id`/`inbox_url`/icon per domain).
- Remove the gate's `discovery: Task 4` entries.
- Tests: Review Focus 3 (no cache bleed, both orders); WebFinger for shop.example's own user and community answers with shop subject and links, hell's answers byte-identical to the golden copy, cross-domain resources 404, onion 404; NodeInfo counts per domain; replace `test_webfinger_answers_only_for_the_default_domain` with the per-domain rule (list the test change in the report).
- Commit: `feat: each federating domain answers WebFinger, host-meta and NodeInfo for itself (C3)`.

### Task 5: Signing, User-Agent and inbound matching per domain

**Files:** instance-level signing sites in recon §2 (`signature.py:466-480`, `actor.py:217`, `activitypub/util.py:1499, 1676, 1977, 6399, 6987`, `user/utils.py:168`, `discovery/credits.py:249-366`, `discovery/instance_actor.py:16`) → `instance_actor_signing(domain)` where the domain is the one acting (the actor's owner for actor-triggered fetches; the default for relays and housekeeping); User-Agent strings (`app/utils.py:375, 377, 440`, `signature.py:857`, `onion_media_fill.py:173`) name the acting domain; `_check_signed_host` / `_target_uri` (`signature.py:348-373`) accept and build per federating host; the "is it ours" prefix tests in recon §1e (`is_local_url` `models.py:58-62`; `routes.py:843, 1117`; `util.py:1078, 4171, 4769, 4986, 6172`; `models.py:3278`; `relays/expiry.py:30`; `actor.py:414`, `routes.py:505, 3773`) accept every federating hosted domain (not local-only ones, not by string prefix — by `domain_of_url`).
- Remove the gate's `User-Agent: Task 5` entries.
- Tests: a signed fetch triggered by a shop user carries shop's keyId and User-Agent (record headers with respx); relay subscription still the default's; an inbound Announce of a shop post is recognised as ours; a Create addressed to both domains' inboxes is processed once (existing Redis dedupe — pin it); `Post.is_local()` true for a shop post with a shop ap_id; a `https://shop.example.evil/...` URL is not ours.
- Commit: `feat: requests a domain's actors make are signed and named as that domain (C3)`.

### Task 6: Media URLs on the owner's domain

**Files:** `app/shared/media.py:127`, `app/shared/upload.py:120`, `app/shared/post.py:697`, `app/shared/tasks/media.py:234` (stored `File.source_url` for new uploads = uploader's domain base + served path); federated icon/image/attachment URLs built at serialize time for local files use the owner's base (`app/models.py:1105-1180` File helpers as needed). Existing stored URLs are not rewritten (they stay the first domain). Caddy already serves the same routes on every site block (piefed_routes), so the URL resolves on any federating domain.
- Remove the gate's `media: Task 6` entries.
- Tests: a shop user's upload stores a shop URL and the post's federated attachment names shop; a hell upload unchanged; the media route answers on the shop host (app-level).
- Commit: `feat: a domain's uploads are linked on that domain (C3)`.

### Task 7: The admin can add a federating domain

**Files:** `app/admin/routes.py:331-352` (drop the refusal of `federates=True` for non-default rows; the C2 lock on `federates` once a domain has owners stays), `Caddyfile` (a commented, ready-to-copy site block for a federating domain using `import piefed_routes`, no `Onion-Location`), `tests/test_caddyfile_routing.py` (the template parses and routes like the first domain's block when uncommented — test the text), `deploy/DEPLOY_GUIDE.md` "Adding a federating domain" (DNS, domain row, Caddy block, reload, first checks: WebFinger, `/actor`, NodeInfo, follow from a remote account) — `deploy/` is local-only.
- Tests: the form creates a federating non-default domain with its key pair and instance row; the lock still refuses flipping `federates` on a domain with owners.
- Commit: `feat: an admin can add a second federating domain (C3)`.

### Task 8: Deploy dark and verify (controller)

- [ ] Before the push: identity before-set on prod (`/u/wakko`, WebFinger, `/c/microblogs`, `/actor`, `/`, `/.well-known/host-meta`, `/.well-known/nodeinfo`, `/nodeinfo/2.1`) to `~/backups/<date>-identity/before/`.
- [ ] After autodeploy: `cmp` — only `updated` and NodeInfo counts differ; the migration gave no key to the default row and a key to the onion row (every non-default row gets one, in case a domain with no owners is switched to federate), and the onion's `/actor` still answers no other server; 0 tracebacks over 10 min in all app units; one first-domain post federates to a remote follower as before (check the outbound log / a remote copy).
- [ ] Update DEPLOY_GUIDE §11 and the memory `onion-storefront-track`; note the live second-domain checks owed when the owner adds one.

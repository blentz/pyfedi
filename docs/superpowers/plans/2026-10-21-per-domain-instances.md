# Per-domain instance rows (plan C2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each hosted domain gets its own `instance` row, so sister-domain content reads as another instance wherever code tests the instance, users can block a sister instance, and each domain browses as its own instance: Local means the request's domain, and hot feeds prefer own > sister > remote.

**Architecture:** `site_domain.instance_id` links each domain to its row; the default keeps row 1. `app/storefront.py` gains the two meanings of "local" as helpers: `hosted_instance_ids()` (any of our domains: storage, delivery, moderation, federation lists) and `current_instance_id()` (the request's domain: Local filters, ranking, badges). Every literal `instance_id == 1` / `!= 1` in app/ is sorted into one of those, and a source-scan gate keeps it that way.

**Tech Stack:** Flask, SQLAlchemy/Postgres, Alembic, Jinja, pytest (`./run_tests.sh`, podman test stack).

**Spec:** docs/superpowers/specs/2026-10-19-per-domain-identity-design.md (§3.1 "Each hosted domain has its own instance row", §3.5). Recon: the C2 recon (S1-S8) quoted in the ledger's pre-flight section.

## Global Constraints

- The first domain's actor, instance actor, WebFinger and community documents stay byte-identical (only `updated` may change). NodeInfo counts may change (they will now count row 1 only).
- The default domain keeps instance row 1. Remote rows are untouched.
- A sister instance row never gets an `inbox`/`shared_inbox`, and no delivery, polling, dormancy or failure bookkeeping ever targets a hosted instance id.
- A local-only domain's instance row is invisible on every other storefront and in every federated list or API: lists omit it, `/instance/<its domain>` 404s. On its own storefront it shows as a sister (owner ruling 2026-10-21). Federating sisters are never listed as "federated".
- Ranking (owner ruling 2026-10-21, "Boost hot sorts"): `hot` and `scaled` add a query-time bonus, own domain +1.0, sister +0.5 (one ranking unit = 12.5 h of age or 10x score; Claude's call on the size). `new`, `old`, `top*` and `active` are unchanged. Community lists and search add own > sister > remote as the last ORDER BY key (tie-break only).
- A user may block a sister instance; nobody may block their own home domain's instance (today's "You cannot block the local instance." message).
- Feeds and emoji stay on row 1 (global, as names).
- TDD, one commit per task or defect, 100% changed-line and branch coverage (`python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`), inline imports only if technically unavoidable, slow tests are bugs. Fuzz targets reaching the new helpers stub them.

## Review Focus

1. A celery task or CLI command (no request): `current_instance_id()` is the default domain's row 1, so every R-site behaves exactly as today on the first domain.
2. A user blocks the onion's instance on the first domain: onion content disappears from their feeds; their own (hell) content never does, and their own instance can't be blocked from any storefront.
3. An onion community post with a hot ranking a little below a first-domain post: on the onion it ranks above it; on the first domain below it.
4. Delivery fan-out for a first-domain post with a follower on the onion: no outbound HTTP to the onion row (it has no inbox), and no failure counter moves.
5. The anonymous `/instances` page and `/api/v3/federated_instances` on the first domain never contain the onion hostname.

---

### Task 1: Each domain has an instance row

**Files:**
- Create: `migrations/versions/<rev>_per_domain_instance.py` (down_revision `b7e3c1a9d5f2`)
- Modify: `app/models.py` (SiteDomain: `instance_id` FK to `instance.id`, not null after backfill), `app/storefront.py` (DomainInfo gains `instance_id`; new `hosted_instance_ids() -> frozenset[int]`, `current_instance_id() -> int`, `instance_id_of(domain: DomainInfo) -> int`), `app/admin/routes.py:331-352` (adding a domain creates its Instance row: `domain`, `software='PieFed'`, no inbox).
- Test: `tests/test_per_domain_instances.py`; update `tests/two_domains.py` so the onion gets its own Instance row and `local_user`/`local_community` use the domain's row.

**Interfaces:**
- Produces: the three helpers above (cached with the domain cache; `clear_domain_cache()` clears them). Outside a request `current_instance_id()` is the default's id (1).

- [ ] **Step 1: failing tests**
  - `test_the_default_domain_keeps_instance_one`
  - `test_adding_a_domain_creates_its_instance_row_without_an_inbox`
  - `test_hosted_instance_ids_are_every_domains_row`
  - `test_current_instance_id_follows_the_storefront_and_is_one_outside_a_request`
  - migration test (pattern of the plan B ownership migration test): with a non-default site_domain, a local user/community on it, and their posts/replies, upgrade gives the domain an Instance row and moves those rows' `instance_id` to it; default-domain rows stay 1; remote rows untouched; downgrade restores 1.
- [ ] **Step 2:** run, FAIL. **Step 3:** implement. Backfill posts and replies by their author's `site_domain_id` (local authors only). **Step 4:** full gate. **Step 5:** commit `feat: each hosted domain has its own instance row (C2)`.

### Task 2: New local rows get their domain's instance

**Files:** every creation path in the recon's S2 table that hard-codes `instance_id=1` for a User or Community (auth/util.py:339-346 and :369-375, auth/oauth_util.py:131-138, admin/routes.py:2312 and :2693, cli.py:289 and :1246, community/routes.py:155-169, shared/community.py:318-325, dev/routes.py:46-53, activitypub/util.py:7293-7303): set `instance_id = instance_id_of(<the domain they set site_domain_id from>)`. Posts and replies already copy `user.instance_id`. `Post.move_to` (models.py:4132) keeps `community.instance_id` — check it is right for a move into a sister community and say so in the report. Feeds, emoji and the transient archived-reply author stay 1.
- Test: `tests/test_per_domain_instances.py`: an admin-added onion user, an onion-created community, and that user's post and reply all carry the onion's instance id; the same paths on hell carry 1.
- Commit: `fix: a new account, community, post or reply on a domain belongs to that domain's instance (C2)`.

### Task 3: "Hosted here" sites use hosted_instance_ids

**Files:** the recon's S1b H-table sites: visibility.py:96/108; models.py:870, 1621, 2581, 5745; admin/util.py:65; user/routes.py:1319; shared/tasks/blocks.py:162, deletes.py:275, pages.py:153/195/478, maintenance.py:354-390 (retention: hosted = local cutoff), :458, :552; activitypub/routes.py:458, 2484/2516; api/alpha/views.py:1521; instance/routes.py:103; utils.py:7255; main/routes.py:709 (modlog trusted) and :801 (sitemap, see ruling below); cli.py:1504/1506; templates post/post.html:12, user/show_profile.html:16, user/show_upvoted.html:16 (indexable meta: hosted AND not local-only); community/routes.py:2177/2179 and admin/routes.py:2462/2464 (report local/remote filter); Report.is_local (models.py:5202); report creation `source_instance_id=1` sites (set the reporter's own instance). `utils.py:5807` JOINED_SERVER_SQL and `find_microblogging_community` stay row 1 (the microblogs community is the default domain's).
- Sitemap ruling: the sitemap lists the request domain's posts (`current_instance_id()`), so the first domain's is unchanged.
- Create the gate `tests/test_instance_id_literals.py`, modelled on tests/test_local_name_lookups.py: an AST/template scan of app/ that flags any comparison or assignment of an instance id to the literal 1 (`instance_id == 1`, `!= 1`, `Instance.id != 1`, `get(Instance, 1)`, `filter_by(id=1)` on Instance, SQL text `instance_id = 1`), with an allowlist where each entry names its reason (`row 1 is the default domain's: <why>`), plus a stale-entry test. Task 4's and Task 5's sites are allowlisted with reason `request-domain site, Task 4` / `blocking site, Task 5` and removed by those tasks.
- Tests by effect: fan-out for a hell post with an onion follower makes no HTTP (`no_http`) and moves no failure counter; `/api/v3/federated_instances` and alpha `federated_instances_view` exclude every hosted row; retention keeps a sister's post on the local cutoff; an onion user's report reads as local.
- Commit: `fix: our other domains count as hosted here for delivery, moderation and federation lists (C2)`.

### Task 4: "Local" means the request's domain

**Files:** S1a/S3 R-sites: main/routes.py:176/178 (home Local SQL) and :1529 (local RSS) use `current_instance_id()` (drop the now-redundant `on_domain_sql` pairing only if the test shows equal results); api/alpha/utils/user.py:106; community/live.py:145; instance/routes.py:144 (`/instance/local/people` = the request domain's row); main/routes.py:759 and activitypub/util.py:181-192 (`local_posts/comments/communities`: about page counts the request's domain; NodeInfo and lemmy site JSON count row 1); post/routes.py:803 emoji order stays row 1 (allowlist: emoji are global). The API reply Local filter's `PostReply.ap_id.startswith('https://'+SERVER_NAME)` (api/alpha/utils/reply.py:136-139) becomes the request domain's authority.
- Remove the `request-domain site, Task 4` allowlist entries.
- Tests: on the onion, the home Local tab, local RSS and API `type_=Local` (users, posts, replies) list only onion rows; on hell only hell rows; the same calls outside a request match today's hell output.
- Commit: `fix: the Local tab and Local filters on a domain show that domain's own content (C2)`.

### Task 5: Blocking a sister instance

**Files:** S5 exemption sites: shared/site.py:14-19, user/routes.py:1006 and :1902, instance/routes.py:351, chat/routes.py:212, post/routes.py:1900 and :2327, templates post/post_options.html:204 and post_reply_options.html:126. "The local instance" becomes the viewer's home domain's instance (`current_user.instance_id`); a sister row is blockable like any remote one.
- Remove the `blocking site, Task 5` allowlist entries.
- Tests by effect: a hell user blocks the onion's instance (web and API) and onion posts leave their home feed and the onion community list; blocking their own instance is refused with the existing message on both storefronts; an onion user may block hell's instance; the "hide instance" option shows on a sister's post and not on one from the viewer's own domain.
- Commit: `feat: a user can block one of our other domains like any instance, never their own (C2)`.

### Task 6: A local-only domain's row stays off other storefronts

**Files:** `/instances` (instance/routes.py:60-110), `/instance/<domain>` overview/people/posts (instance/routes.py:127-136 and siblings), modlog instance dict (main/routes.py:690-715), `fediverse_domains` (utils.py:7255), both federated-instances APIs, discovery/seed and any listing the recon S6 names. Add one predicate in app/storefront.py, `instance_visible_here(instance_id) -> bool`: false for the row of a domain with `federates=False` unless the request's storefront is that domain. The admin instances page shows everything (admins).
- Tests: anonymous and logged-in `/instances`, `/api/v3/federated_instances` and the alpha view on hell contain no onion hostname; `/instance/<onion>` and its people/posts pages 404 on hell and render on the onion; a future federating sister (fixture `shop.example` from tests/test_per_domain_ap_routes.py) is listed but not under the federated filter.
- Commit: `fix: the onion's instance never shows on another domain (C2)`.

### Task 7: Hot feeds prefer the domain's own content

**Files:** app/utils.py `get_deduped_post_ids` (:5964-6000) and `post_ids_to_models` (:6008-6026), `instance_sticky_posts` (:6038) if it sorts hot; api/alpha/utils/post.py hot/scaled sorts (:569-570, :639-642, :1182, :1221, :1271, :1312). Add in app/storefront.py `domain_boost_sql(alias: str) -> tuple[str, dict]` giving `CASE WHEN <alias>.instance_id = :own_instance THEN 1.0 WHEN <alias>.instance_id IN :sister_instances THEN 0.5 ELSE 0 END` and its params (constants `OWN_DOMAIN_BOOST = 1.0`, `SISTER_DOMAIN_BOOST = 0.5`); the SQLAlchemy-expression twin for the API. Hot: `ORDER BY p.ranking + <boost> DESC, p.posted_at DESC`; scaled: `p.ranking_scaled + <boost> DESC, p.ranking DESC, p.posted_at DESC`. Community lists (main/routes.py:1818 `safe_order_by`, api/alpha/utils/community.py:183-195) and search (search/routes.py:129-132, 187-190) append the tier as the last ORDER BY key.
- Tests: the Review Focus 3 pair on both storefronts, hot and scaled; `new`/`top`/`active` order unchanged by domain; a hell community list with two otherwise-equal communities lists hell's first on hell and the onion's first on the onion; with no sisters and no request, the hot order equals today's for row-1 vs remote posts offset by exactly the boost (pin one case).
- Commit: `feat: hot feeds on each domain lift that domain's own posts, then its sister domains' (C2)`.

### Task 8: Deploy and verify (controller, after the owner pushes)

- [ ] Before the push: save the identity set to `~/backups/<date>-identity/before/` (DEPLOY_GUIDE §11), plus `select id, domain, inbox from instance where id = 1 or domain in (select domain from site_domain)` and the onion's user/community/post counts by instance_id.
- [ ] After autodeploy: identity `cmp` (only `updated`, NodeInfo counts differ); the onion has its own instance row with no inbox and its rows moved to it; 0 tracebacks in web/celery/beat/background/notifs over 10 min; anonymous `/instances` on hell has no onion hostname.
- [ ] Owner: on the onion while logged in, the home hot feed shows onion posts lifted; blocking the first domain's instance from an onion account hides hell posts.
- [ ] Update DEPLOY_GUIDE §11 (sister instance rows, blocking, ranking) and the memory `onion-storefront-track`.

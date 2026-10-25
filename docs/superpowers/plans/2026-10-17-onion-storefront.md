# Onion Storefront Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PieFed answers on several registered domains, keeps each visitor on the storefront they arrived on, and the first domain gets a Tor onion service that is fast, abuse-resistant and never leaks clearnet URLs.

**Architecture:** A `site_domain` table and a per-request `g.storefront` decide every presentation URL. Federation keeps using the canonical `SERVER_URL`. A response-level link rewriter is the safety net for stored HTML. Onion traffic is marked by Caddy and trusted only from Caddy's pinned address, and it gets per-session rate limits, proof-of-work and per-account login delays instead of IP rules. The deploy adds a single-onion `tor` container, a Caddy onion listener and fail2ban jails.

**Tech Stack:** Flask 3.1.3, Flask-SQLAlchemy, Alembic (flask-migrate), Flask-Caching 2.5, Flask-Limiter, Flask-WTF 1.3, BeautifulSoup 4.15, Redis, pytest via `./run_tests.sh`; podman quadlets, Caddy 2, tor ≥ 0.4.8 with PoW, fail2ban.

**Spec:** `docs/superpowers/specs/2026-10-17-onion-storefront-design.md` (commit f59ed084b). Section refs (§) below point into it.

## Global Constraints

- Federation identity never varies by request host. ActivityPub IDs, `keyId`, WebFinger and NodeInfo use `current_app.config['SERVER_URL']` / `SERVER_NAME` (§2 anti-goals).
- An onion page must not reference `https://example.org` in any `href`, `src`, `srcset`, `action`, `content`, `Location` or SSE URL (§2).
- Clearnet behaviour is unchanged unless a task says otherwise.
- Inline imports only if technically unavoidable. Break cycles by moving code (memory `inline-imports-rule`).
- TDD: failing test first. One commit per task. Run tests with `./run_tests.sh <path> -v`, never from `/tmp` scripts (memory `gates-live-in-repo`).
- **Coverage: 100% line and branch coverage on every changed line** (owner ruling 2026-10-17), base `b20e75bfe`. Every app task ends with this chain passing (tests/README.md "coverage ratchet"):
  ```
  ./run_tests.sh tests/ -q --cov=app --cov-branch --cov-report=json && \
  python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini && \
  python3 tests/check_changed_line_coverage.py coverage.json b20e75bfe --branches
  ```
  New modules `app/storefront.py`, `app/onion.py`, `app/onion_guard.py` get floor 100 in `coverage_floors.ini`. Task 10 adds `app/static/js/onion_pow.js` to `package.json` `test:js` `--test-coverage-include` at 100 lines/branches/functions; its tests are `tests/js/onion_pow.test.mjs`.
- No new inline imports: `KEPT` in `tests/test_no_inline_imports.py` must not rise.
- Tests must leave no file under `app/static/media`, `app/static/tmp` or `app/static/posts` (`tests/static_leak_gate.py`).
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, or names the model that actually wrote it.
- A slow or timing-out test is a bug to fix, not to report (memory `slow-tests-are-bugs`).
- `deploy/` is git-ignored and local-only. Deploy-task files are not committed. Back up unit files, env files and the Caddyfile to `~/backups/<UTC timestamp>-onion/` on the server before editing. Pushing to GitHub is the owner's job.
- With an empty `site_domain` table, the `SERVER_NAME` host is implicitly the one registered default. This keeps the existing ~657 test files and fresh installs working.
- Defaults pinned by the spec: unknown host → 421. No-JS wait 15 s. Login per-account delay doubling and capped. Upload limit unchanged at 510MiB in both Caddy blocks.

## Review Focus

1. **Host header variants**: `EXAMPLE.ORG`, `example.org.`, `x.onion:80` should all resolve to the same storefront. Test in Task 2.
2. **Stored-link variants**: `http://example.org/…`, `//example.org/…`, `https://EXAMPLE.ORG/…`, and `srcset` lists should be canonicalised on save and rewritten on render. Tests in Task 6.
3. **POST-redirect-GET on the onion**: a form POST whose view redirects to an absolute `SERVER_URL` URL should send a `Location` on the onion host. Test in Task 6.
4. **Password-reset or verification email requested over the onion**: the link should use the onion host, because it was sent during the request. Test in Task 4b.
5. **Signed ActivityPub inbox POST arriving on the onion host** (a Tor-capable remote server): it should be accepted. Signature verification should use the request's own Host, and stored IDs stay canonical. Test in Task 4a.

---

## Phase A: domain registry and host-aware URLs (spec §3)

### Task 1: `SiteDomain` model, migration, cached lookup

**Files:**
- Modify: `app/models.py` (add `SiteDomain` next to `Site`, ~:5301)
- Create: `migrations/versions/<rev>_site_domain.py` (`down_revision` = current head; check with `flask db heads`, last seen `e5b8d2f0a4c7`)
- Create: `app/storefront.py`
- Test: `tests/test_storefront_domains.py`

**Interfaces:**
- Produces, in `app/storefront.py`:
  - `DomainInfo = NamedTuple('DomainInfo', domain: str, scheme: str, federates: bool, registration: str, is_default: bool, is_onion: bool)`
  - `normalise_host(host: str) -> str`: lower-case, port stripped, trailing dot stripped.
  - `domain_rows() -> tuple[DomainInfo, ...]`: memoized with `@cache.memoize(timeout=300)`. When the table is empty it returns one synthesised default from `SERVER_NAME` (scheme: `https` if `HTTP_PROTOCOL` is `https` or `mixed`, else `HTTP_PROTOCOL`).
  - `clear_domain_cache() -> None`
  - `domain_for_host(host: str) -> DomainInfo | None`
  - `default_domain() -> DomainInfo`
  - `is_registered_host(host: str) -> bool`
- `SiteDomain` columns are exactly as in §3.1. `registration` is a `String(10)` with a check constraint `IN ('anyone','staff','admin')`. Partial unique index `ix_site_domain_one_default ON site_domain (is_default) WHERE is_default`.

- [ ] **Step 1: Write failing tests**
  - `test_empty_table_synthesises_default`: no rows → `domain_rows()` is a single row with `domain == 'test.piefed.local'`, `is_default`, `scheme == 'https'`.
  - `test_second_default_rejected`: inserting two `is_default=True` rows raises `IntegrityError`.
  - `test_domain_for_host_normalises`: rows `test.piefed.local` and `abc.onion` → `domain_for_host('ABC.onion:80')` and `domain_for_host('abc.onion.')` both return the onion row, with `is_onion is True`. `domain_for_host('evil.example') is None`.
  - `test_mixed_protocol_default_is_https`: with `HTTP_PROTOCOL='mixed'`, the synthesised default has `scheme == 'https'`, and `SERVER_URL` is unchanged (§3.6, upstream `mixed` keeps working).
  - `test_cache_cleared_on_change`: add a row, call `clear_domain_cache()`, and the new row is visible.
  - `test_migration_seeds_from_env`: in the migration's `upgrade()`, one row equal to the synthesised default. `registration` maps from `site.registration_mode`: `Open`→`anyone`, `RequireApplication`→`anyone`, `Closed`→`admin`. Test the mapping function `registration_from_mode(mode: str) -> str`, which lives in `app/storefront.py`; the migration copies the mapping inline, as migrations must not import app code.
- [ ] **Step 2: Run** `./run_tests.sh tests/test_storefront_domains.py -v`. Expect FAIL on import.
- [ ] **Step 3: Implement** the model, the migration and `app/storefront.py`. `is_onion` is set in a `@validates('domain')` hook from `domain.endswith('.onion')`. Startup (`create_app`, after the DB is ready, guarded for CLI) logs `current_app.logger.error` when the default row's domain ≠ `SERVER_NAME`.
- [ ] **Step 4: Run** the tests. Expect PASS. Then run `./run_tests.sh` in full. Expect no new failures.
- [ ] **Step 5: Commit** `feat: site_domain table lists every domain the site answers on`

### Task 2: Resolve the storefront per request

**Files:**
- Modify: `app/request_hooks.py` (new `before_request` registered **before** `cap_media_upload`, ~:107)
- Modify: `app/storefront.py`
- Test: `tests/test_storefront_request.py`

**Interfaces:**
- Consumes: Task 1's `domain_for_host`, `default_domain`.
- Produces:
  - `current_storefront() -> DomainInfo`: `g.storefront` inside a request, else `default_domain()`.
  - `storefront_url() -> str`: `f"{scheme}://{domain}"` of `current_storefront()`.
  - `canonical_url() -> str`: returns `current_app.config['SERVER_URL']`.
  - `g.storefront: DomainInfo`, set for every request on a known host.

- [ ] **Step 1: Write failing tests**
  - `test_unknown_host_is_421`: `client.get('/', headers={'Host': 'evil.example'})` gives status 421, a body with no HTML, and no `Location`.
  - `test_known_alias_resolves`: with an onion row, `GET /` on Host `abc.onion` gives 200, and inside `app.test_request_context(base_url='http://abc.onion')`, `storefront_url() == 'http://abc.onion'`.
  - `test_host_variants` (Review Focus 1): `HELL`-style upper case, trailing dot, and `:80` all give 200 and the same `g.storefront.domain`.
  - `test_url_for_external_follows_host`: in a request on `abc.onion`, `url_for('main.index', _external=True)` starts with `http://abc.onion/`. Outside a request, under `app.app_context()`, it starts with `https://test.piefed.local/`. If Flask 3.1 pins it to `SERVER_NAME`, make it follow the host by setting `request.url_root`-based adapters, not by removing `SERVER_NAME`. Record which mechanism worked in the commit message.
  - `test_static_and_inbox_on_alias`: `GET /static/...` and `POST /inbox` on Host `abc.onion` are not 421.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement** `resolve_storefront()` as the first `before_request`. It does `abort(421)` with a plain-text response for an unregistered host.
- [ ] **Step 4: Run** the tests and then the full suite. Expect PASS and no new failures.
- [ ] **Step 5: Commit** `feat: each request is served as the storefront its Host names`

### Task 3: Admin "Domains" section

**Files:**
- Modify: `app/admin/routes.py` (new `@bp.route('/domains', methods=['GET','POST'])` beside `/site` at :138)
- Modify: `app/admin/forms.py`
- Create: `app/templates/admin/domains.html`
- Modify: `app/templates/admin/_nav.html`
- Test: `tests/test_admin_domains.py`

**Interfaces:**
- Consumes: `SiteDomain`, `clear_domain_cache()`.
- Produces: the route `admin.admin_domains`.

- [ ] **Step 1: Write failing tests**
  - `test_non_admin_forbidden`: a staff user gets 403.
  - `test_add_domain`: an admin posts a new row `{domain: 'ABC.onion', scheme: 'http', federates: on, registration: 'staff'}`. Stored as `abc.onion`, `is_onion`, not default. The cache is cleared, so `domain_for_host('abc.onion')` is not None.
  - `test_invalid_domain_rejected`: `'https://x.y'`, `'x y'`, `''` and a duplicate are refused with a form error, and nothing is stored.
  - `test_default_radio_locked`: the page renders the default radio `disabled` with the note text. A forged POST that sets `is_default` on another row leaves the `SERVER_NAME` row as default (§3.1, until project 3).
  - `test_cannot_delete_default`: deleting the default row is refused.
  - `test_page_says_later_release_and_dns`: the rendered page contains "takes effect in a later release" next to the federate and registration controls, and a link to `deploy/DEPLOY_GUIDE.md` text about DNS/TLS/Tor (§3.2).
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.** Domain validation: an IDNA-encodable hostname matching `^[a-z0-9.-]+$` after lower-casing, with no scheme and no port.
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `feat: admins list the site's domains in site settings`

### Task 4a: Sort federation and local-ness call sites

**Files:**
- Modify: `app/activitypub/**`, `app/models.py` (ID generators), `app/utils.py` (local-ness checks), and any `is_local`-style checks that compare against `SERVER_NAME`
- Create: `tests/storefront_url_inventory.py` (data: allowed direct uses)
- Test: `tests/test_storefront_url_inventory.py`, `tests/test_storefront_federation.py`

**Interfaces:**
- Consumes: `is_registered_host`, `canonical_url`.
- Produces: `CANONICAL_USES: dict[str, dict[str, int]]`, mapping repo-relative file → enclosing function name (or `'<module>'`) → count of `SERVER_URL`/`SERVER_NAME` reads. A use listed here is a deliberate federation or identity use.

- [ ] **Step 1: Write failing tests**
  - `test_every_direct_use_is_inventoried`: AST-walk `app/**/*.py` and count `config['SERVER_URL']`, `config['SERVER_NAME']`, `config.get('SERVER_…')` per (file, function). Every count must equal `CANONICAL_USES` exactly. The failure message names each mismatch with its file and function, and says to classify it per §3.4.
  - `test_local_link_on_alias_is_local`: a post body linking `http://abc.onion/post/<id>` resolves as a local post, using the same function that today recognises `https://test.piefed.local/post/<id>`. Find it by grepping for `SERVER_NAME` in `app/utils.py` and `app/post/`.
  - `test_actor_json_on_alias_is_canonical`: `GET /u/alice` with `Accept: application/activity+json` on Host `abc.onion` returns `id`, `inbox`, `outbox`, `publicKey.id`, all starting `https://test.piefed.local/`.
  - `test_webfinger_on_alias_is_canonical`: same as above for `/.well-known/webfinger?resource=acct:alice@test.piefed.local`.
  - `test_signed_inbox_on_alias` (Review Focus 5): with the `signing_peer` fixture and `tests/factories.py`'s `signed_inbox_post`, a Follow signed for Host `abc.onion` and posted with that Host gets 200/202 and is stored.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.** Classify every direct use module by module. Federation and identity uses stay as they are and go into `CANONICAL_USES`. Presentation uses are left uninventoried, so they fail this test until Tasks 4b and 4c convert them. Mark the test `xfail(strict=True)` with reason `"presentation uses pending Tasks 4b/4c"` until Task 4c, which removes the marker. Every check of "is this URL ours" uses `is_registered_host`.
- [ ] **Step 4: Run.** The federation tests should PASS and the inventory test should XFAIL. Then run the full suite.
- [ ] **Step 5: Commit** `refactor: federation uses of SERVER_URL are inventoried; local links on any registered domain count as local`

### Task 4b: Presentation call sites: pages, media, feeds, emails

**Files:**
- Modify: `app/models.py` (`File.view_url` and siblings ~:1037, avatar/banner/thumbnail URL helpers), `app/utils.py`, the RSS/Atom views, OpenGraph and `<link rel=canonical>` template contexts, the manifest view, and the email helpers called during requests
- Test: `tests/test_storefront_presentation.py`

**Interfaces:**
- Consumes: `storefront_url()`.

- [ ] **Step 1: Write failing tests** (each on Host `abc.onion`, with fixtures from `tests/factories.py`)
  - `test_page_urls_stay_on_storefront`: GET each of `/`, a community, a post with an image, a user profile, `/auth/login`, `/auth/register`, `/user/settings` (logged in), and `/search?q=x`. Parse each with BeautifulSoup. Every absolute URL in `href`, `src`, `srcset`, `action`, and `meta[content]` that has a host must have host `abc.onion`. Assert the list of offenders is `[]`.
  - `test_media_url_on_storefront`: `File.view_url` for a local file is `http://abc.onion/static/media/...`.
  - `test_feed_on_storefront`: the community RSS has `<link>` and enclosure URLs on `abc.onion`.
  - `test_reset_email_uses_storefront` (Review Focus 4): POST a password-reset request on `abc.onion`. The captured email body links to `http://abc.onion/`.
  - `test_background_email_uses_default`: a celery-path email (e.g. `email_notifs`) built outside a request links to `https://test.piefed.local/`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.** Replace each presentation read with `storefront_url()`. Do not add to `CANONICAL_USES`.
- [ ] **Step 4: Run.** Expect PASS. Then run the full suite.
- [ ] **Step 5: Commit** `feat: pages, media, feeds and request-time emails use the visitor's storefront`

### Task 4c: Presentation call sites: API, JS config, streams; finish inventory

**Files:**
- Modify: `app/api/alpha/**` (URLs returned for display), the template or JS config that tells `app/static/js/` the notification and live stream URLs, and the remaining uninventoried uses
- Modify: `tests/test_storefront_url_inventory.py` (remove `xfail`)
- Test: `tests/test_storefront_api.py`

- [ ] **Step 1: Write failing tests**
  - `test_api_post_view_on_storefront`: the alpha API post view on `abc.onion` returns display URLs (`thumbnail_url`, `url` of local images) on `abc.onion`, and `ap_id` on `test.piefed.local`.
  - `test_stream_urls_relative_or_storefront`: the rendered page's stream config holds no `test.piefed.local` when served on `abc.onion`.
  - The inventory test with `xfail` removed.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite. Expect PASS. `rg "SERVER_URL|SERVER_NAME" app` shows only inventoried uses.
- [ ] **Step 5: Commit** `feat: API display URLs and stream config follow the storefront; every SERVER_URL use is classified`

### Task 5: Cache keys include the storefront

**Files:**
- Modify: `app/storefront.py`, every `@cache.cached` presentation view (19 sites, `grep -rn "@cache.cached" app`), and every `@cache.memoize` that returns presentation URLs (audit the 87 sites)
- Test: `tests/test_storefront_cache.py`

**Interfaces:**
- Produces: `storefront_cache_key() -> str`, used as a `key_prefix` callable: `f"view/{current_storefront().domain}{request.path}"`. `app/utils.py:523` `make_cache_key` already includes the host via `request.url`. Leave it as is and add a test that pins it.

- [ ] **Step 1: Write failing tests**
  - `test_cached_view_per_host`: with the real cache backend, GET a cached presentation view on `test.piefed.local` and then on `abc.onion`. The second response contains `abc.onion` and not `test.piefed.local`.
  - `test_ap_cached_view_shared`: the ActivityPub cached routes (`app/activitypub/routes.py:268-328`) return byte-identical bodies on both hosts. These keep their path keys on purpose.
  - `test_make_cache_key_includes_host`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.** Write the audit result for the memoize sites as a short table in the commit body (site, and whether it returns URLs). Fix the ones that do.
- [ ] **Step 4: Run** the full suite. Expect PASS.
- [ ] **Step 5: Commit** `fix: cached pages are cached per storefront`

### Task 6: Stored links: canonical on save, storefront on render

**Files:**
- Modify: `app/utils.py` (`allowlist_html` :1068, which `markdown_to_html` :2032 and federated HTML both pass through)
- Modify: `app/storefront.py`, `app/request_hooks.py` (an `after_request`)
- Test: `tests/test_storefront_links.py`

**Interfaces:**
- Produces:
  - `canonicalise_links(html: str) -> str`: rewrites `href`/`src`/`srcset` whose host is registered to `canonical_url()`. It is called at the end of `allowlist_html`.
  - `rewrite_links(body: str, storefront: DomainInfo) -> str`: rewrites the same attributes, plus `action` and `meta[content]`, from any registered host to `storefront`. It returns `body` unchanged, without parsing, when no registered domain string appears in it.
  - An `after_request` hook `rewrite_response_for_storefront(response)`: only when `g.storefront.domain != default_domain().domain`. It runs `rewrite_links` on `text/html` bodies that are not streamed, and rewrites an absolute `Location` header whose host is registered.

  This is the render-time rewrite of §3.5, done once on the response instead of a Jinja filter in each of the ~18 `|safe` sites. The spec leaves the mechanism to the builder. It also catches any presentation URL Tasks 4b and 4c missed.

- [ ] **Step 1: Write failing tests**
  - `test_canonicalise_variants` (Review Focus 2): the inputs `http://abc.onion/post/1`, `//abc.onion/post/1`, `https://ABC.onion/post/1`, and `srcset="http://abc.onion/a.jpg 1x, http://abc.onion/b.jpg 2x"` all come out on `https://test.piefed.local`. `https://example.com/x` and the visible text `abc.onion` are unchanged.
  - `test_rewrite_skips_when_absent`: patch the parser and check it is not called for HTML with no registered domain.
  - `test_onion_page_has_no_canonical_links`: a post whose stored `body_html` links `https://test.piefed.local/post/1`, viewed on `abc.onion`, has the link on `http://abc.onion/post/1`.
  - `test_location_rewritten` (Review Focus 3): on `abc.onion`, a logged-in POST to a view that redirects to an absolute `SERVER_URL` URL (pick one with grep, e.g. post creation) gets a `Location` on `http://abc.onion/`.
  - `test_clearnet_response_untouched`: on the default host, `rewrite_links` is never called.
  - `test_sse_not_buffered`: a streamed response passes through unread.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement** with BeautifulSoup over `html.parser` and a cheap substring precheck. Never edit stored DB rows.
- [ ] **Step 4: Run** the full suite. Expect PASS.
- [ ] **Step 5: Commit** `feat: stored links are saved canonical and shown on the visitor's storefront`

### Task 7: Secure cookies over plain-http onion

**Files:**
- Modify (only if the spike says so): `app/__init__.py` or a session interface override
- Test: `tests/test_storefront_cookies.py`

- [ ] **Step 1: Spike, recorded in the commit body.** Use a throwaway local onion: a `tor` container with `HiddenServicePort 80 host.containers.internal:<port>`, in front of `flask run`. In current Tor Browser, check whether a `Set-Cookie: …; Secure` sent over `http://<onion>` is stored and sent back.
- [ ] **Step 2: Write tests.** Always: `test_session_cookie_host_only`, which checks the `Set-Cookie` on `abc.onion` has no `Domain=`. Only if the spike says Secure cookies fail: `test_secure_flag_per_storefront`, which checks `Secure` is present on https storefronts and absent on `http` onion storefronts.
- [ ] **Step 3: Implement** only what the spike requires.
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `test: session cookies are host-only on every storefront` (plus the fix, if one was needed)

## Phase B: onion abuse handling (spec §3.7, §3.7a)

### Task 8: Onion marker and IP semantics

**Files:**
- Create: `app/onion.py`. It must import nothing from `app`, so `app/__init__.py` can import it without a cycle.
- Modify: `app/__init__.py` (`get_ip_address` :59, `limiter` :118), `config.py` (`ONION_PROXY_IP = os.environ.get('ONION_PROXY_IP')`), `app/utils.py` (`block_honey_pot` :2915, the honeypot route, `get_country`, and the `user.ip_address` writes)
- Test: `tests/test_onion_marker.py`

**Interfaces:**
- Produces:
  - `is_onion_request() -> bool`: true only when `request.headers.get('X-PieFed-Via') == 'onion'`, `ONION_PROXY_IP` is set, and `request.environ['werkzeug.proxy_fix.orig']['REMOTE_ADDR'] == ONION_PROXY_IP`. Outside a request it is false.
  - `rate_limit_key() -> str`: for an onion request, `f"onion-session:{session['onion_sid']}"`; otherwise `get_ip_address()`. It becomes the `Limiter` key function.
  - `get_ip_address()` returns `''` for onion requests.

- [ ] **Step 1: Write failing tests**
  - `test_marker_trusted_from_proxy`: with `ONION_PROXY_IP='10.89.0.2'`, a request with `environ_base={'REMOTE_ADDR': '10.89.0.2'}`, `X-Forwarded-For: 10.89.0.7` and the header is onion.
  - `test_marker_ignored_from_client`: the same request from `REMOTE_ADDR='203.0.113.9'` is not onion.
  - `test_marker_ignored_when_unset`: with `ONION_PROXY_IP` unset, it is never onion. An onion domain registered with it unset logs a startup warning.
  - `test_onion_no_ip_ban`: an IP ban on `''` or on the proxy IP does not block an onion request.
  - `test_onion_honeypot_ends_session`: an onion hit on the honeypot route clears the session and sets no `ban:*` key in Redis (use the `redis_double` fixture).
  - `test_onion_signup_ip_null`: registration over the onion stores `user.ip_address is None`. Geolocation is not called.
  - `test_clearnet_unchanged`: the existing IP-ban test still passes.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.** `session['onion_sid']` is set to `secrets.token_urlsafe(16)` on the first onion request.
- [ ] **Step 4: Run** the full suite. Expect PASS.
- [ ] **Step 5: Commit** `feat: onion requests are recognised from Caddy only and never use the shared Tor IP`

### Task 9: Session-first, per-session limits, per-account login delay

**Files:**
- Create: `app/onion_guard.py`
- Modify: `app/request_hooks.py`, `app/auth/util.py` (`process_login` :458, `validate_user_login` :510), `app/shared/auth.py` (`api_log_user_in`)
- Test: `tests/test_onion_guard_limits.py`

**Interfaces:**
- Consumes: `is_onion_request()`.
- Produces:
  - `ONION_SESSION_ENDPOINTS: frozenset[str]`: every endpoint with a `@limiter.limit` (from `grep -rn "@limiter.limit" app`).
  - `login_delay_remaining(username: str) -> int`: seconds the caller must still wait.
  - `record_onion_login_failure(username: str) -> None`
  - `clear_onion_login_failures(username: str) -> None`
  - Redis key `onion_login_fail:<lower username>` holds `{count, last}`, with a 1 h TTL. The first 5 failures are free. After that the wait is `min(2 ** (count - 5), 300)` seconds from `last`. These numbers are my call within the spec's "doubling, capped".

- [ ] **Step 1: Write failing tests**
  - `test_cookieless_get_redirects`: an onion `GET /auth/login` with no cookie gets 303 to the same URL with a `Set-Cookie`.
  - `test_cookieless_post_refused_softly`: an onion `POST /auth/login` with no cookie gets 303 to the login form and a flash saying to try again. It never reaches `process_login`.
  - `test_clearnet_cookieless_unchanged`.
  - `test_session_bucket`: two onion sessions each get their own `10 per 5 minutes` login bucket. Session A hitting 429 does not affect session B.
  - `test_login_delay_grows`: 6 onion failures for `alice` give a 2 s wait. 7 give 4 s. The wait never exceeds 300 s. During the wait, a correct password is refused with a "try again in N seconds" flash.
  - `test_delay_onion_only`: clearnet login for `alice` succeeds while the onion delay is active.
  - `test_delay_covers_api`: alpha API `/user/login` over the onion honours the delay with 429.
  - `test_success_clears`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite. Expect PASS.
- [ ] **Step 5: Commit** `feat: onion rate limits are per session, and failed onion logins slow that account down`

### Task 10: Proof-of-work and the no-JS wait step on login and registration

**Files:**
- Modify: `app/onion_guard.py`, `app/auth/forms.py`, `app/auth/routes.py` (`login` :47, `register` :68), the login and register templates
- Create: `app/static/js/onion_pow.js`, `app/templates/auth/onion_wait.html`
- Modify: `app/admin/forms.py` and the `/misc` view (:326) to add an `onion_pow_difficulty` setting, default 16
- Test: `tests/test_onion_pow.py`, `tests/js/onion_pow.test.mjs`

**Interfaces:**
- Produces:
  - `issue_challenge() -> str`: an `itsdangerous.URLSafeTimedSerializer(SECRET_KEY, salt='onion-pow')` token over `{salt: token_hex(16), bits: int}`. It is valid for 10 minutes.
  - `verify_pow(token: str, nonce: str) -> bool`: the signature and age are valid, `sha256(f"{salt}:{nonce}".encode())` has at least `bits` leading zero bits, and the Redis key `onion_pow_used:<salt>` does not exist yet. It sets that key with `SET NX` and a 600 s TTL.
  - `issue_wait_token() -> str` and `wait_token_ready(token: str) -> bool`: ready when the age is between 15 and 600 s, and the token is single-use in the same way.
  - Hidden form fields `pow_token`, `pow_nonce` and `wait_token`. They are rendered and checked only when `is_onion_request()`.

- [ ] **Step 1: Write failing tests**
  - `test_valid_solution_accepted`: solve in Python at `bits=8`, and login proceeds.
  - `test_expired`, `test_bad_signature`, `test_reused`, `test_unsolved`: each is refused with a form error, and the password is not checked.
  - `test_wait_token_too_early_then_ok`: freeze time (use the suite's existing time helper). The token is refused at 14 s and accepted at 15 s.
  - `test_noscript_link_present`: the onion login page has a `<noscript>` link to the wait step. The wait page has `<meta http-equiv="refresh" content="15;url=…">`.
  - `test_clearnet_has_no_pow`: the clearnet login page has no `pow_token`, and a POST without one succeeds.
  - JS test: `solve(token, bits)` finds a nonce that the Python `verify_pow` accepts (shared vector).
  - `test_pow_js_self_hosted`: the page's `<script src>` is `/static/js/onion_pow.js`, a relative path or on the storefront host.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.** The JS uses `crypto.subtle.digest('SHA-256', …)`, with no library. It solves in a loop and submits when done. Builder's call: vendor ALTCHA instead only if writing it ourselves turns out to be worse.
- [ ] **Step 4: Run** the full suite. Expect PASS.
- [ ] **Step 5: Commit** `feat: onion login and registration ask for a small proof-of-work, or a wait without JavaScript`

### Task 11: Failed-login log line for fail2ban

**Files:**
- Modify: `app/auth/util.py` (each `flash(invalid_login_message())` path, :482 and :512–518), `app/shared/auth.py` (API failure path)
- Test: `tests/test_login_failure_log.py`

- [ ] **Step 1: Write a failing test**: `test_failed_login_logs_ip_not_username`. A wrong password from `203.0.113.9` logs exactly `auth: login failed ip=203.0.113.9` at WARNING, and the record contains neither the username nor the email. An onion failure logs `ip=onion`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement** with `current_app.logger.warning`.
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `feat: failed logins are logged with the client IP for fail2ban`

## Phase C: Tor deploy for the first domain (spec §4)

The owner pushes Phases A and B to the deploy branch first. Wait for autodeploy, then check: `curl -sI https://example.org/` gives 200, a post page renders, and `journalctl -u pyfedi-web --since -10min | grep -c Traceback` is 0. Do not start Phase C until clearnet is confirmed unchanged.

### Task 12: Pinned network and a static Caddy IP

**Files (local-only):** `deploy/quadlet/pyfedi.network`, `deploy/quadlet/pyfedi-caddy.container`. Server: `.env.docker` and the regenerated `.env.quadlet`.

- [ ] **Step 1:** On the server, run `podman network inspect pf_network`. Record the current subnet.
- [ ] **Step 2:** Add `Subnet=<that subnet>` and `Gateway=` to `pyfedi.network`, so the change does not move other containers. Add `IP=<subnet>.250` to `pyfedi-caddy.container`. Add `ONION_PROXY_IP=<subnet>.250` to `.env.docker` and regenerate `.env.quadlet` (memory `first-domain-quadlet-deploy`).
- [ ] **Step 3:** Back up, install, `systemctl daemon-reload`, and restart the stack in the documented order.
- [ ] **Step 4: Verify.** `podman inspect pyfedi_caddy --format '{{.NetworkSettings.Networks}}'` shows `.250`. `podman exec piefed_app1 printenv ONION_PROXY_IP` matches. The first domain gives 200.

### Task 13: Tor container, key volume, key backup

**Files (local-only):** create `deploy/tor/Containerfile`, `deploy/tor/torrc`, `deploy/quadlet/pyfedi-tor.container`, `deploy/quadlet/pyfedi-tor-data.volume`, `deploy/quadlet/pyfedi-tor.build`.

- [ ] **Step 1:** Write `Containerfile`: a distro base pinned by digest, the `tor` package, and `USER` set to tor's own user.
- [ ] **Step 2:** Write `torrc` with exactly the §4.1 and §4.4 directives: `HiddenServiceDir /var/lib/tor/hs_pyfedi`, `HiddenServiceVersion 3`, `HiddenServicePort 80 pyfedi_caddy:8081`, `HiddenServiceNonAnonymous 1`, `HiddenServiceSingleHopMode 1`, `SocksPort 0`, `HiddenServicePoWDefensesEnabled 1`, `HiddenServiceEnableIntroDoSDefense 1`, `Log notice stdout`.
- [ ] **Step 3: Build and check PoW.** `podman run --rm localhost/pyfedi-tor tor --version` shows ≥ 0.4.8. `tor --list-modules` shows `pow: yes`. If not, switch to building tor from source with `--enable-gpl` in the same Containerfile, and record why.
- [ ] **Step 4:** Back up, install, and start `pyfedi-tor`. Verify that `journalctl -u pyfedi-tor` shows `Bootstrapped 100%` and that `podman exec pyfedi_tor cat /var/lib/tor/hs_pyfedi/hostname` prints a 56-character `.onion`.
- [ ] **Step 5: Back up the key.** `podman exec pyfedi_tor cat /var/lib/tor/hs_pyfedi/hs_ed25519_secret_key | age -r <recipient of DB dumps> > ~/backups/tor/hs_ed25519_secret_key.age`. Check that no plaintext copy exists outside the volume.
- [ ] **Step 6:** Ask the owner to add the onion row in admin → Domains (scheme `http`, federate unchecked, registration as they choose).

### Task 14: Caddy onion listener and Onion-Location

**Files:** `Caddyfile` (tracked in the repo, and deployed from the checkout).

- [ ] **Step 1:** Move the example.org block's body into a snippet `(piefed_routes)`. Keep `encode`, `request_body max_size 510MiB`, both SSE `handle` blocks and the `reverse_proxy web:5000` retry. Keep every existing comment, moved with its directive.
- [ ] **Step 2:** In `example.org { … }`, add `request_header -X-PieFed-Via`, `header Onion-Location http://<onion>{uri}` and `import piefed_routes`.
- [ ] **Step 3:** Add `http://<onion>:8081 { request_header X-PieFed-Via onion; import piefed_routes }`.
- [ ] **Step 4: Validate.** `podman run --rm -v ./Caddyfile:/etc/caddy/Caddyfile:z docker.io/library/caddy:2 caddy validate --config /etc/caddy/Caddyfile` passes.
- [ ] **Step 5: Commit** `deploy: Caddy serves the onion storefront and advertises it with Onion-Location`. The owner pushes it. After the deploy, restart `pyfedi-caddy` only if autodeploy did not.
- [ ] **Step 6: Verify.**
  - `curl -sI https://example.org/ | grep -i onion-location`.
  - `curl -s -H 'X-PieFed-Via: onion' https://example.org/` is treated as clearnet: the login page has no `pow_token`.
  - From inside the tor container, `curl -s http://pyfedi_caddy:8081/` returns the onion-host page.

### Task 15: fail2ban jails

**Files (local-only):** create `deploy/fail2ban/jail.d/pyfedi.local` and `deploy/fail2ban/filter.d/pyfedi-{login,register,scan,tor}.conf`. Also `deploy/fail2ban/action.d/pyfedi-alert.conf`, which calls the autodeploy `notify.py` and rate-limits itself to once an hour.

- [ ] **Step 1:** Write the filters.
  - `pyfedi-login` matches `auth: login failed ip=<HOST>` from `journalmatch = _SYSTEMD_UNIT=pyfedi-web.service`.
  - `pyfedi-register` matches gunicorn `"POST /auth/register` lines.
  - `pyfedi-scan` matches 4xx on `/wp-`, `/.env`, `/phpmyadmin`, `/cgi-bin`, `/xmlrpc.php`.
  - `pyfedi-tor` matches tor's real DoS and PoW notices, taken from tor's source or docs. Record them in the filter.
  - Every web filter has an `ignoreregex` for `/inbox`.
- [ ] **Step 2: Dry-run** each filter against a day of real logs: `fail2ban-regex systemd-journal[journalmatch='_SYSTEMD_UNIT=pyfedi-web.service'] <filter>`. Record the hit counts. There must be no `/inbox` hits and no remote ActivityPub server IPs among the hits.
- [ ] **Step 3:** Write the jails.
  - Thresholds: `pyfedi-login` bans after 10 in 10 min for 1 h. `pyfedi-register` bans after 20 in 10 min for 1 h. `pyfedi-scan` bans after 20 in 5 min for 1 h. `pyfedi-tor` has `action = pyfedi-alert` with no ban.
  - `ignoreip = 127.0.0.1/8 ::1 <pf_network subnet>`.
  - Ban action: one that works on DNATed podman traffic. Try `banaction = nftables[type=multiport]` with the chain set to netavark's forward path, or `firewallcmd-rich-rules` on the `FORWARD` hook. Keep whichever passes Step 4.
- [ ] **Step 4: Prove the ban.**
  - From an outside host the owner names, check that `curl -sI https://example.org/` works.
  - Run `fail2ban-client set pyfedi-login banip <that IP>` and check that the curl now times out.
  - Run `unbanip` and check that it works again.
  - Record the result.
- [ ] **Step 5:** Back up `/etc/fail2ban`, install the jails and reload. `fail2ban-client status` lists the 4 new jails.

### Task 16: End-to-end check and docs

**Files:** `deploy/DEPLOY_GUIDE.md` (local-only)

- [ ] **Step 1:** In Tor Browser, open the onion address with the network panel open.
  - Browse the index, a community and a post.
  - Register (PoW visible), or log in with a test account.
  - Post with an image upload.
  - Receive a live notification from a second account.
  - Pass: the network panel shows zero requests to `example.org`.
- [ ] **Step 2:** Repeat the login at the "Safest" security level. Pass: the wait step works.
- [ ] **Step 3:** Open `https://example.org` in Tor Browser. Pass: the ".onion available" prompt appears.
- [ ] **Step 4:** Add an onion section to `DEPLOY_GUIDE.md`:
  - the address
  - the key backup path, and how to restore it (stop `pyfedi-tor`, decrypt into the volume's `hs_pyfedi/`, `chown` to tor, `chmod 600`, start)
  - the `ONION_PROXY_IP` dependency on Caddy's static IP
  - the jail list
- [ ] **Step 5:** Update memory `first-domain-quadlet-deploy` with the new units, volume, key backup and jails.

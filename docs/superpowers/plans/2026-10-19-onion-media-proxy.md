# Onion Remote-Media Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On onion storefronts, every remote media URL in an HTML page is served from the onion through a signed,
SSRF-guarded, disk-cached proxy. Remote iframe embeds become a placeholder plus a link.

**Architecture:** The existing onion response rewrite (`app/storefront.py`, one BeautifulSoup pass) swaps remote
media URLs for `/onion_media/<sig>/<key>`. A new module `app/onion_media.py` owns signing, cache paths, streamed
fills and eviction. The route serves a cache hit by `X-Accel-Redirect`, which Caddy intercepts and serves with
`file_server`. Images fill in the request, at most 2 at once. Video, audio and image overflow fill on a Celery
`onion_media` queue.

**Tech Stack:** Flask, BeautifulSoup, httpx (via `app/pinned_http.py`), Redis, Celery, Caddy 2, podman quadlets.

**Spec:** `docs/superpowers/specs/2026-10-19-onion-media-proxy-design.md`

## Global Constraints

- Onion storefronts only (`storefront.is_onion`). Clearnet and non-onion storefront responses are byte-identical to today.
- The server fetches only URLs it signed. Signature = HMAC-SHA256 of the remote URL, key derived from `SECRET_KEY`
  with salt `onion-media`. No expiry.
- SSRF: every hop goes through `is_invalid_get_request_uri` and the `pinned_http` transport. Redirect cap 5. http/https only.
- Types: `image/*`, `video/*` and `audio/*` only. `image/svg+xml` refused.
- Defaults (admin settings): `onion_media_cache_bytes` 10 GiB (10737418240), `onion_media_image_max_bytes`
  20 MiB (20971520), `onion_media_av_max_bytes` 500 MiB (524288000).
- In-request image fills: at most 2 at once across all workers, with a 15 s deadline. Negative cache 1 h.
- Eviction: least recently used, down to 90% of the cap. A hit refreshes the file's mtime with `os.utime`.
- Cache dir: config `MEDIA_PROXY_DIR`, default `media_proxy` under the app root (`/app/media_proxy` in the
  container). Never inside `media/`.
- Proxied responses: `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`.
- TDD. One commit per task, message `feat: …`/`fix: …` in plain words, ending
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Each new test < 1 s, and no real network.
- Gate after each task, from the repo root:
  `./run_tests.sh tests/ -q --cov=app --cov-branch --cov-report=json && python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini && python3 tests/check_changed_line_coverage.py coverage.json <BASE> --branches`
  where `<BASE>` is the commit before Task 1.
- Inline imports only if technically unavoidable. Match the surrounding comment density and naming.
- Fuzz contract: campaigns never touch the DB or network. Stub `storefront.domain_rows` and the fill.

## Review Focus

1. A remote URL in `srcset` with several candidates and descriptors (`a.jpg 1x, b.jpg 2x`): each candidate is
   proxied and the descriptors are kept. Pinned in Task 2.
2. A redirect to `http://127.0.0.1/` or to a pf_network address (10.89.0.x) from an allowed host: refused at that
   hop, nothing stored. Pinned in Task 3.
3. A remote server that answers `text/html` (or `image/svg+xml`) on an image URL: refused, never served from the
   onion origin. Pinned in Task 3.
4. A tampered `<sig>` or `<key>`, or a valid pair on clearnet: 404 with no fetch. Pinned in Task 4.
5. Two onion visitors on a cold 29-image page: no more than 2 in-request fills run, and the rest answer 503 with
   `Retry-After` and are queued once each. Pinned in Task 4.

---

### Task 1: Signed proxy URLs and cache paths

**Files:**
- Create: `app/onion_media.py`
- Modify: `config.py` (add `MEDIA_PROXY_DIR`)
- Test: `tests/test_onion_media_urls.py`

**Interfaces:**
- Produces:
  - `proxy_url(remote: str) -> str`: the relative URL `/onion_media/<sig>/<key>`. `<key>` is
    `urlsafe_b64encode(remote.encode())` with `=` padding stripped. `<sig>` is the hex HMAC.
  - `remote_for(sig: str, key: str) -> str | None`: the URL when `sig` matches, else None. It uses
    `hmac.compare_digest` and never raises.
  - `cache_name(remote: str, content_type: str) -> str`: `sha256(remote).hexdigest()` plus the extension for an
    allowed type, from `EXTENSIONS = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
    'image/webp': '.webp', 'image/avif': '.avif', 'image/apng': '.apng', 'video/mp4': '.mp4',
    'video/webm': '.webm', 'video/ogg': '.ogv', 'audio/mpeg': '.mp3', 'audio/ogg': '.ogg', 'audio/mp4': '.m4a',
    'audio/wav': '.wav', 'audio/webm': '.weba', 'audio/flac': '.flac'}`. Ruling: the type is the file extension, so
    Caddy's `file_server` sends the right `Content-Type`. This replaces the spec's "type saved beside".
  - `cached_file(remote: str) -> pathlib.Path | None`: the existing cache file for `remote`, under any allowed
    extension.

- [ ] **Step 1: Write the failing tests**
  - `test_round_trip`: `remote_for(*proxy_url(u).split('/')[2:4]) == u` for `https://lemmy.zip/pictrs/image/a.webp`
    and for a URL with a query and non-ASCII path.
  - `test_tampered_signature_or_key_is_none`: flip one character of each. Result None.
  - `test_signature_depends_on_secret_key`: with a different `SECRET_KEY`, `remote_for` is None.
  - `test_cache_name_uses_the_type_extension`: `image/avif` gives `.avif`. An unknown type raises `KeyError`.
- [ ] **Step 2: Run them.** `./run_tests.sh tests/test_onion_media_urls.py -q`. Expect FAIL (no module).
- [ ] **Step 3: Implement** in `app/onion_media.py`. Use `itsdangerous`-free `hmac.new(key, remote.encode(), sha256)`,
  where `key = sha256(f"{SECRET_KEY}:onion-media".encode()).digest()`.
- [ ] **Step 4: Run.** Expect PASS. Run the gate.
- [ ] **Step 5: Commit** `feat: signed onion media proxy URLs and their cache file names`.

### Task 2: The onion rewrite proxies remote media and replaces remote embeds

**Files:**
- Modify: `app/storefront.py` (`_rebase_links`, `rewrite_links`, `rewrite_response_for_storefront`)
- Test: `tests/test_storefront_media_proxy.py`

**Interfaces:**
- Consumes: `proxy_url` (Task 1); the existing `browser_host`, `_srcset_urls`, `_css_urls`, `_placeholder`, `is_local_host`.
- Produces: `rewrite_links(body, storefront)` proxies on onion storefronts. New private helper
  `_proxy_remote(soup, tag) -> bool`, called in the same `find_all` loop after `_hide_s3_or_the_tag`, and failing
  closed the same way (`_proxy_remote_or_the_tag`).

Rules (from spec §3.1):
- Remote = absolute http(s) URL, `not is_local_host(browser_host(url))`, and not the S3 host (S3 hiding wins).
- Rewritten: `src`, `poster`, `data-*` URL values and `style` `url(...)` on `img`, `video`, `audio`, `source`,
  `track` and `picture`; and every candidate in `srcset`.
- `a href` is rewritten only when the path ends in an extension of `EXTENSIONS` (case-insensitive) or contains
  `/pictrs/image/`.
- A remote `iframe`, `embed` or `object` becomes `_placeholder(soup)` followed by
  `<a href="{remote}" rel="nofollow noreferrer">{remote}</a>`.
- On an onion storefront the parse is never skipped (as with `s3_host` set).

- [ ] **Step 1: Write the failing tests** (Host `abc.onion`, fixtures as in `tests/test_storefront_s3_media.py`)
  - `test_remote_img_is_proxied`: the `src` becomes `proxy_url(src)`.
  - `test_srcset_candidates_are_proxied_with_descriptors` (Review Focus 1).
  - `test_media_link_is_proxied_page_link_is_not`: `a href=.../pictrs/image/x.webp` is proxied. `a href=https://lemmy.world/post/1` is unchanged.
  - `test_remote_iframe_becomes_placeholder_and_link`: no `iframe` is left, there is a `span.onion-media-hidden`, and the `a` is the embed URL.
  - `test_style_background_url_is_proxied`.
  - `test_local_and_s3_urls_untouched_by_the_proxy`: local URLs are left alone, and S3 URLs are still hidden.
  - `test_onion_page_has_no_remote_media_host`: a page with every case above has no remote host left in any
    `src`/`srcset`/`poster`/media `href`/iframe.
  - `test_clearnet_page_is_byte_identical`: the same body on the default storefront is unchanged.
  - `test_a_failure_hides_the_element`: when `proxy_url` raises, the element becomes the placeholder and the error is logged.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement** `_proxy_remote` and wire it into `_rebase_links` with a new `proxy: bool` parameter.
  `rewrite_links` passes `proxy=storefront.is_onion`. Keep the docstrings in step.
- [ ] **Step 4: Run.** Expect PASS. Then run `tests/test_storefront_*.py` and the gate.
- [ ] **Step 5: Commit** `feat: onion pages load remote media through the onion and show remote embeds as a link`.

### Task 3: Streamed, guarded cache fill

**Files:**
- Modify: `app/onion_media.py`, `app/utils.py` (only to expose a streaming sibling of `_capped_request`, if needed)
- Test: `tests/test_onion_media_fill.py`

**Interfaces:**
- Consumes: `cache_name`, `cached_file` (Task 1); `is_invalid_get_request_uri`, `instance_banned`, `Domain.banned`,
  `pinned_http.pinned_transport`, `fetch_deadline`.
- Produces:
  - `fill(remote: str, *, max_seconds: float) -> pathlib.Path | None`: fetches `remote` into the cache and returns
    its path, or None when refused or failed. A refusal sets the negative-cache key
    `onion_media:dead:<sha256>` (ex 3600). Raises nothing.
  - `is_dead(remote: str) -> bool`.
  - `kind(content_type: str) -> str | None`: `'image'`, `'av'` or None (refused).

Behaviour:
- Up to 5 redirects. Each `Location` is resolved against the current URL and re-checked with
  `is_invalid_get_request_uri`, `instance_banned(host)` and a banned `Domain` row before connecting.
- Refuse on `Content-Type` not in `EXTENSIONS` (SVG is not in it), and on a declared `Content-Length` over the
  type's cap (`onion_media_image_max_bytes` / `onion_media_av_max_bytes` via `get_setting`).
- Stream the body to `MEDIA_PROXY_DIR/.tmp-<uuid>` and stop past the cap. On success `os.replace` it to
  `cache_name`, and on any failure unlink it. No cookies and no `Referer`. The normal PieFed User-Agent.
- A file larger than `onion_media_cache_bytes` is refused.

- [ ] **Step 1: Write the failing tests** (an `httpx.MockTransport` behind the pinned-transport seam, `tmp_path` as `MEDIA_PROXY_DIR`)
  - `test_image_fills_and_names_by_type`.
  - `test_redirect_hops_are_followed_and_checked`: 301 to another host works, and the 6th hop is refused.
  - `test_redirect_to_private_address_refused` (Review Focus 2): `127.0.0.1` and `10.89.0.250`. Nothing is
    stored, and `is_dead` is True.
  - `test_html_and_svg_refused` (Review Focus 3).
  - `test_over_cap_refused_declared_and_streamed`: by `Content-Length`, and by a body that exceeds the cap without one.
    No `.tmp-*` is left behind.
  - `test_banned_instance_and_domain_refused`.
  - `test_no_cookie_or_referer_sent`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect PASS. Run the gate.
- [ ] **Step 5: Commit** `feat: the onion media cache fills by a streamed, SSRF-guarded fetch`.

### Task 4: The `/onion_media` route and the Celery fill

**Files:**
- Create: `app/onion_media_routes.py` (blueprint `onion_media`, registered in `app/__init__.py`)
- Modify: `app/onion_media.py` (task), `app/__init__.py` (`CELERY_ROUTES`: `'app.onion_media.fill_task': {'queue': 'onion_media'}`)
- Test: `tests/test_onion_media_route.py`

**Interfaces:**
- Consumes: `remote_for`, `cached_file`, `fill`, `is_dead`, `kind` (Tasks 1 and 3).
- Produces: `GET /onion_media/<sig>/<key>`; the Celery task `fill_task(remote: str)`; and
  `queue_fill(remote: str) -> None`, which de-duplicates with the Redis key `onion_media:queued:<sha256>`
  (`set nx`, ex 600).

Behaviour:
- Not onion: 404. A bad signature: 404. `is_dead`: 404.
- Hit: refresh the mtime (`os.utime`) and answer 200 with an empty body and the header
  `X-Accel-Redirect: /<file name>`, plus `nosniff` and the CSP header.
- Miss: decide by the URL's extension (the type is not known before the fetch). Image or unknown: try the Redis
  semaphore `onion_media:inflight` (at most 2, each slot ex 20). When a slot is free, `fill(max_seconds=15)` runs
  in the request and serves as a hit, or answers 404 when it returns None. When none is free, `queue_fill` and
  answer 503 with `Retry-After: 10`. Video or audio extension: `queue_fill` and 503 with `Retry-After: 30`.
- `fill_task` calls `fill(remote, max_seconds=600)`.

- [ ] **Step 1: Write the failing tests**
  - `test_clearnet_and_bad_signature_404_without_fetch` (Review Focus 4). Patch `fill` to fail if it is called.
  - `test_hit_answers_accel_redirect_and_refreshes_mtime`.
  - `test_image_miss_fills_in_request`.
  - `test_third_concurrent_image_miss_is_queued_503` (Review Focus 5). Two slots are held, and the third request gets 503 with `Retry-After` and is queued once even when requested twice.
  - `test_video_miss_is_queued_never_fetched_in_request`.
  - `test_dead_url_404`.
  - `test_fill_task_routes_to_onion_media_queue`: `celery.conf.CELERY_ROUTES` names it.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect PASS. Run the gate.
- [ ] **Step 5: Commit** `feat: the onion serves remote media from its cache, filling images in the request and the rest in the background`.

### Task 5: Admin settings and eviction

**Files:**
- Modify: `app/admin/forms.py` (`SiteMiscForm`), `app/admin/routes.py` (misc save and load, beside `onion_pow_difficulty`),
  `app/templates/admin/misc.html`, `app/onion_media.py`, `app/cli.py`
- Create: `evict_onion_media.sh` (same shape as `remove_orphan_files.sh`)
- Test: `tests/test_onion_media_evict.py`

**Interfaces:**
- Produces: `evict(cap_bytes: int) -> int` (bytes freed): when the cache total exceeds `cap_bytes`, it deletes
  the oldest-mtime files until the total is at most `cap_bytes * 0.9`, and removes stale `.tmp-*` older than 1 h.
  CLI `flask evict_onion_media` calls it with `onion_media_cache_bytes`.
- The admin fields are in MiB on the form and stored in bytes: cache 1024–1048576, image max 1–100, video/audio max 1–2048.

- [ ] **Step 1: Write the failing tests**
  - `test_evicts_least_recently_used_to_90_percent`.
  - `test_under_cap_evicts_nothing`.
  - `test_stale_tmp_files_removed_fresh_ones_kept`.
  - `test_admin_misc_saves_and_shows_the_three_settings`: bounds are enforced, and a value out of range is refused.
  - `test_cli_command_uses_the_setting`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect PASS. Run the gate.
- [ ] **Step 5: Commit** `feat: the onion media cache has an admin size cap and least-recently-used eviction`.

### Task 6: Fuzz target

**Files:**
- Modify: `tests/fuzz/targets/urls.py` (or a new `tests/fuzz/targets/onion_media.py`, registered in `tests/fuzz/surfaces.py`), plus its corpus dir
- Test: the smoke run

**Interfaces:**
- Target `onion_media_rewrite`: random HTML through `rewrite_links` on an onion `DomainInfo`, with `domain_rows`
  stubbed (`_no_domain_rows()`) and S3 unset. Invariants: no exception; no `iframe` with a remote `src` survives;
  every `src`/`srcset` candidate is relative, local, or `proxy_url`-shaped; `remote_for` round-trips each proxied value.
- Target `onion_media_signature`: random `(sig, key)` gives `remote_for` returning None or a str, without raising.

- [ ] **Step 1:** Write the targets and a small seed corpus (img, srcset, iframe, style url, a href).
- [ ] **Step 2:** Run `python -m tests.fuzz.smoke --seconds 20` in the test-runner (as in the gaps work). Expect all targets clean.
- [ ] **Step 3: Commit** `test: fuzz the onion media rewrite and its signature check`.

### Task 7: Deploy (local-only sources plus the tracked Caddyfile; the owner pushes)

**Files:**
- Modify: `Caddyfile` (tracked), `deploy/quadlet/pyfedi-web.container`, `deploy/quadlet/pyfedi-celery-background.container`,
  `deploy/quadlet/pyfedi-caddy.container`, `deploy/DEPLOY_GUIDE.md` §11
- Create: `deploy/systemd/pyfedi-job@evict_onion_media.timer`

- [ ] **Step 1: Caddyfile.** In `(piefed_routes)`, inside `reverse_proxy web:5000 { … }`, add the following (it is
  harmless on clearnet, because the route is onion-only):
  ```
  @accel header X-Accel-Redirect *
  handle_response @accel {
      root * /srv/media_proxy
      rewrite * {rp.header.X-Accel-Redirect}
      header X-Content-Type-Options nosniff
      header Content-Security-Policy "default-src 'none'; sandbox"
      file_server
  }
  ```
  Validate with `caddy validate`. Commit `deploy: Caddy serves cached onion media files itself`.
- [ ] **Step 2: Units.**
  - Add `Volume=/home/rocky/pyfedi/media_proxy:/app/media_proxy:z` to web and celery-background.
  - Add `Volume=/home/rocky/pyfedi/media_proxy:/srv/media_proxy:ro,z` to Caddy.
  - Set `CELERY_QUEUES=background,onion_media` on celery-background.
  - Add a timer that runs `pyfedi-job@evict_onion_media.service` every 15 min.
  - Back up to `~/backups` before installing. Then `mkdir ~/pyfedi/media_proxy`, owned by rocky.
  - Install, `daemon-reload`, and restart the stack after the owner's push has deployed Tasks 1–5.
- [ ] **Step 3: Verify.**
  - From the tor-like curl (`--connect-to <onion>:80:10.89.0.250:8081`), `/c/memes@lemmy.world` has no remote
    media host in `src`/`srcset`.
  - A proxied image URL gives 200 with an image `Content-Type`, `nosniff` and the CSP header.
  - A `Range: bytes=0-99` request on a cached video gives 206.
  - The same page on clearnet is unchanged.
  - 0 tracebacks.
  - The owner repeats the Tor Browser HAR check: 0 requests to any host but the onion.
- [ ] **Step 4:** Update DEPLOY_GUIDE §11 (cache dir, settings, timer, queue) and the `first-domain-quadlet-deploy` memory.

# Onion remote-media proxy: design

Date: 2026-10-19. Follows `docs/superpowers/specs/2026-10-17-onion-storefront-design.md` (the onion storefront).

## 1. Intent

A Tor Browser test of the onion storefront (HAR of `/c/memes@lemmy.world`, 2026-10-18) showed onion pages loading
29 images straight from other fediverse instances: `lemmy.zip`, `images.lemmy.zip`, `pictrs.blahaj.zone`,
`lemmy.world`, `startrek.website`, `media.infosec.exchange`. Tor carries those requests, so no visitor IP leaks, and
Tor Browser sent no `Referer`. But each remote server still sees an onion visitor fetching its media, and links to
that media take the visitor off the onion.

Owner's goal: **an onion visitor's browser talks only to the onion.** Our server fetches remote media for it and
serves it from the onion.

## 2. Goals, non-goals, anti-goals

Goals (owner rulings 2026-10-19)
- Onion storefronts only. Clearnet pages are unchanged ("onion only").
- Every kind of remote media an onion page shows or links to: post images and teaser thumbnails, avatars,
  community icons and banners, emoji and reactions, inline images in post and comment bodies, and **video and
  audio** ("proxy video and audio too").
- **On-disk cache with a size cap** ("cache on disk with a size cap").

Non-goals
- Clearnet proxying, and hiding visitors' IPs from remote instances on clearnet.
- Proxying non-media links (articles, remote post pages). Those stay ordinary links. Following one leaves the onion,
  and that is the visitor's choice.
- S3-hosted media of our own. The onion already hides it (the earlier gaps work). Unchanged.
- Media in non-HTML responses: API JSON, RSS, and anything JavaScript builds from JSON. This matches the S3 hiding,
  which also rewrites HTML pages only. It is listed as a later item. (Claude's call.)

Anti-goals (a failure even if it "works")
- An **open proxy**: our server fetching any URL a visitor names.
- An **SSRF path**: a fetch that reaches loopback, private, link-local or pf_network addresses, directly or by a
  redirect.
- A proxy that **starves the web workers**. Production gunicorn is gthread with 4 workers × 2 threads (8 request
  slots), so one video streamed through Flask per slot would take the site down.
- A cache that fills the disk. The host has 133 GB free, and media already uses 27 GB.
- Any onion HTML page that still makes a request to a remote host for media covered by §2 Goals, or that loads a
  remote embed.

## 3. Design

### 3.1 Rewriting (where)
- The existing onion response rewrite (`app/storefront.py`, `rewrite_response_for_storefront` → `rewrite_links`,
  the same single BeautifulSoup pass that hides S3 media) rewrites each **remote media URL** to an onion-local
  proxy URL. The attributes covered: `src`, `srcset` (each candidate), `poster`, `data-*` URL attributes on
  media elements, `<source src>`, and `href` on links whose target is a media file (the teaser image links in the
  HAR). (Claude's call: one place, so every page is covered, matching how S3 hiding already works.)
- "Remote" means an absolute http(s) URL whose host is not one of ours (`is_local_host` false) and not the S3 public
  host (that one is hidden, §2 non-goals).
- "Media" for `href`: a URL whose path ends in a known image/video/audio extension, or a `/pictrs/image/` path.
  A plain link to a remote page is not touched.
- Inline `style` URLs (`background-image`) on media elements: rewritten the same way. The S3 CSS tokeniser already
  finds them.
- Only onion storefronts: the hook runs on every non-default storefront, so the rewrite checks `storefront.is_onion`,
  as `_s3_host_to_hide` does. Clearnet and any other non-onion storefront are never rewritten.
- **Remote embeds** (owner ruling 2026-10-19, "Placeholder + plain link"): an `<iframe>` whose `src` is remote, such
  as the YouTube (nocookie), Vimeo, Streamable and Redgifs players in `app/templates/post/_post_full.html`, is replaced
  by the "Media hidden on the onion site" placeholder with an ordinary link to the embed's page. Nothing loads unless
  the visitor clicks. Clicking leaves the onion, by the visitor's choice.

### 3.2 Proxy URL and signing (no open proxy)
- `GET /onion_media/<sig>/<key>`, where `<key>` is the remote URL encoded as URL-safe base64, and `<sig>` is an
  HMAC-SHA256 of the URL under a key derived from `SECRET_KEY` with its own salt. (Claude's call.)
- The route is onion-only: `abort(404)` on clearnet, the same as `/auth/onion_wait`.
- A bad signature gives 404. The server fetches only URLs it signed itself.
- No expiry on the signature. Pages are cached, and a URL in a page must keep working. The cache cap bounds the cost.

### 3.3 Fetching (cache fill)
- Reuse the existing SSRF guard: `is_invalid_get_request_uri` plus `pinned_http`'s pinned transport and
  `fetch_deadline`. http and https only. Each redirect hop (cap 5) is a new connection, so it is checked again.
  (Claude's call: the guard already covers DNS rebinding and the D1358 address cases.) `_capped_request` reads the
  whole body into memory, so the fill needs a new variant on the same transport that streams to a temporary file in
  the cache directory and enforces the size cap while it reads. A finished file is renamed into place.
- Refuse URLs on banned instances and banned domains, as the site already does elsewhere. The route does that
  lookup. The rewrite never reads the DB, which keeps the fuzz contract. (Claude's call.)
- Accept `image/*`, `video/*` and `audio/*` only, by the response `Content-Type`. Refuse `image/svg+xml`: SVG can
  carry script and further fetches. (Claude's call.)
- Size caps per file (Claude's call, admin settings): images 20 MiB, video/audio 500 MiB (the upload limit).
- No cookies are sent, and no `Referer`. The User-Agent is the normal PieFed one, the same as federation fetches.
  Remote servers see hell.cloud's IP, as they already do for federation. They never see a Tor exit or a visitor.
- **Images: fetched in the request** on a cache miss, with a 15 s deadline, but **at most 2 in-request fills at
  once** across all workers (a Redis semaphore). A miss beyond that is queued to Celery like video and answers 503,
  so one cold page of 29 images cannot hold the site's 8 request slots. (Claude's call.)
- **Video and audio: never fetched in the request.** A miss queues a Celery task on a new `onion_media` queue
  to fill the cache. The existing celery-background worker consumes that queue too, so fills do not wait behind long
  background jobs (Claude's call; a deploy change to the worker's `-Q` list). The request then
  answers `503` with `Retry-After`. The rewritten `<video>`/`<audio>` keeps its `controls`, so a
  reload after a short wait plays the file. Concurrent misses for one URL queue one task, not several. (Claude's
  call: this keeps web slots free. The cost is the first viewer of a remote video waits.)
- A failed or refused fetch is remembered briefly (negative cache, 1 h) so a dead URL does not refetch on every view.

### 3.4 Serving (Caddy, not Flask)
- Cached files live in a dedicated directory, `/home/rocky/pyfedi/media_proxy/` on the host, bind-mounted
  into the web, celery-background and Caddy containers. It is separate from `media/`, so orphan cleanup never
  touches it and eviction never touches user uploads.
- A cache hit answers with an internal-redirect header naming the cached file. Caddy's onion site intercepts it with
  `reverse_proxy` → `handle_response` and serves the file with `file_server`. Caddy then handles `Range`, `ETag`
  and `Content-Length`, and no gunicorn slot stays busy while the bytes go out. (Claude's call: the
  `X-Accel-Redirect` pattern. Caddy's clearnet site does not need the intercept, because the route is onion-only.)
- Stored files are named by the SHA-256 of the URL, with the content type saved beside them, so Caddy sends the
  right `Content-Type`. `X-Content-Type-Options: nosniff` is always set.
- A file is served under the onion origin, so it must never be HTML or script. §3.3's type allowlist plus `nosniff`
  plus a `Content-Security-Policy: default-src 'none'; sandbox` header on proxied responses guard this.

### 3.5 Cache cap and eviction
- An admin setting `onion_media_cache_bytes`, default 10 GiB. (Claude's call on the value.)
- Least recently used first. A hit refreshes the file's access time (written explicitly, not relying on mount
  `atime`). A periodic job evicts down to 90% of the cap. It is a new `pyfedi-job@` timer, every 15 min, like the
  existing jobs. (Claude's call.)
- One file larger than the cap is never cached. It is refused like an over-size fetch.

### 3.6 Failure display (Claude's call)
- When the proxy cannot serve a URL (refused type, too large, fetch failed, banned, negative-cached), the route
  answers 404, and the browser shows its broken-media box. Pages are cached, so the rewrite cannot know in advance
  that a fetch will fail. A not-yet-filled URL answers 503 (§3.3).

## 4. Constraints
- TDD, one commit per logical change, 100% changed-line and branch coverage. The gates run from repo-committed
  tools. Slow tests are bugs: tests stub the network and never fetch for real.
- Inline imports only if technically unavoidable.
- Fuzz harness contract: campaigns never touch the DB or network. A fuzz target for the URL rewrite and the
  signature check stubs both.
- Deploy: a new bind mount in three quadlet units (web, celery-background, Caddy), the `onion_media` queue on the
  celery-background worker, a Caddyfile change on the onion site, a new job timer. All are local-only `deploy/` sources, plus the tracked Caddyfile. The owner pushes.

## 5. Left to the builder
- The exact media-extension list for `href`, and how `srcset` candidates are re-joined.
- The Celery task's de-duplication mechanism (a Redis lock keyed by the URL hash is the obvious one).
- Test layout. A test checks that an onion page with remote media has no remote host in any `src`, `srcset`,
  `poster` or media `href`, and that the same page on clearnet is byte-identical to today.

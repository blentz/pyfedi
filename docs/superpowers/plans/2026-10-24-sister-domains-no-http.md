# Sister domains without HTTP (plan C4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Our domains never talk to each other over HTTP: no activity is POSTed to, and no ActivityPub object is fetched from, any domain this server hosts. A sister's URL resolves from the database, on the right domain, and remote followers keep getting activity from the originating domain's actor.

**Architecture:** Delivery and lookup already branch on `is_local()` and the hosted-instance filters (recon, 2026-10-24: no path POSTs to a sister today), but only two invariants keep it so: fan-out lists drop hosted `Instance` rows, and hosted rows and local communities carry no inbox. C4 adds a last-line guard at the HTTP choke points, then closes the self-fetch paths by resolving hosted URLs locally with one helper, `local_object_for_url(url)`. Nothing changes on the wire to remote servers.

**Tech Stack:** Flask, SQLAlchemy/Postgres, Celery, httpx, pytest (`./run_tests.sh`).

**Spec:** docs/superpowers/specs/2026-10-19-per-domain-identity-design.md §3.3 ("Between sister domains, no HTTP"), §5 (required test: a sister-domain reply stored without HTTP and delivered to a remote follower signed by the right domain).

## Global Constraints

- Spec §3.3: "A hell.foo user commenting on a hell.cloud post is stored directly. Remote followers of either side get the activity from the originating domain's actor, exactly as if the sister had received it over the network."
- "Exactly as if over the network" (Claude's call, from upstream PieFed): a PieFed user's comment goes to the community (whose Announce reaches the community's followers), mentioned/parent authors, and — only for followers-only replies — the author's followers. C4 keeps that fan-out; it does not add the author's followers for public comments.
- Hosted = every SiteDomain row (federating or local-only) plus SERVER_NAME: `storefront.domain_of_url(url) is not None`.
- C3 rulings stand: own domain only; 404 for an AP fetch of something another of our domains owns; local-only domains answer no other server; relays subscribed by the default domain only.
- hell.cloud byte-identical (actor, WebFinger, NodeInfo, community, instance actor); existing stored ids never rewritten.
- Non-ActivityPub GETs (link previews, image downloads, oEmbed) are out of scope: they are ordinary web fetches, not federation.
- TDD, one commit per task or defect, 100% changed-line and branch coverage (`python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`), inline imports only if technically unavoidable, slow tests are bugs, fuzz targets reaching new helpers stub them, invalid client input stays a 400.

## Review Focus

1. A shop.example URL in search or the API resolve endpoint that is not the exact stored `ap_id` (`/c/den/p/5/slug`, `/post/5` asked on the wrong sister, trailing slash, `HTTP://SHOP.EXAMPLE/post/5`): found from the DB, never fetched (Task 3 tests).
2. A Celery retry of an old queued delivery whose inbox is now on a hosted domain (e.g. a domain added after the row was queued): refused and logged, not sent (Task 1 test).
3. Deleting a post by a user who follows sister-domain accounts: no Delete goes to the people the author follows, sister or remote (Task 2 test).
4. `use_allowlist` on with no hosted domain on the allowlist: sister actors, communities and search still work (Task 4 test).
5. An empty local community added to a feed or imported in settings: no GET of its own moderators/outbox URLs (Task 3 test).

---

### Task 1: Nothing is POSTed to or fetched over ActivityPub from a hosted domain

**Files:** `app/activitypub/signature.py` (`post_request` — after `refused_outbound`, before any network call; `signed_get_request`, `signed_get_capped`), `app/activitypub/util.py:6379` (`fetch_object_capped`), `app/utils.py` (`get_request` / `get_request_capped` only when called with an ActivityPub Accept header — leave HTML/image GETs alone). New `tests/test_no_http_to_hosted.py`.

**Interfaces:** Produces `storefront.is_hosted_uri(uri: str | None) -> bool` (`domain_of_url(uri) is not None`; scheme/case/port/trailing-dot normalised like `domain_of_url`). A refused POST writes one `ActivityPubLog(direction='out', result='failure', exception_message='hosted domain, not sent: <uri>')` and returns without HTTP; a refused AP fetch returns `(None, 'hosted domain')` / None as each function's miss shape, and logs at info.

- [ ] Tests (httpx mocked to fail the test on any call): `post_request` to `https://shop.example/inbox`, to `https://hell.cloud/inbox`, to the onion inbox, and to `HTTPS://Shop.Example:443/u/a/inbox` → no HTTP, one failure log row; to a remote inbox → sent; a SendQueue retry row for a hosted inbox → refused; `fetch_object_capped`/`signed_get_request` of a hosted object → miss without HTTP; an HTML `get_request` of a hosted URL (link preview) still goes out.
- [ ] Commit: `feat: no activity is sent to or fetched from one of our own domains (C4)`.

### Task 2: Delivery fan-outs never aim at our own domains

**Files:** `app/shared/tasks/deletes.py:296-301` (the instance query joins `UserFollower` on `local_user_id == author` with no direction: add `UserFollower.is_inward == True` — a remote user following the author — and `Instance.id.not_in(hosted_instance_ids())`), `app/admin/util.py:79-101` and `app/user/utils.py:93-113` (`unsubscribe_from_community`: skip `community.is_local()` — the membership row is removed, no Leave is sent).

- [ ] Tests: a local author following a sister user and a remote user deletes a post → the Delete goes to the post's community followers and the author's remote followers only, never to the instance of someone the author follows; deleting an account subscribed to a hell.cloud and a shop.example community → no `post_request` call, no `empty uri` log row, memberships gone; a remote community still gets its Leave.
- [ ] Commit: `fix: a delete or unsubscribe never addresses one of our own domains (C4)`.

### Task 3: A hosted URL resolves from the database

**Files:** `app/federation_urls.py` (new `local_object_for_url`), callers: `app/activitypub/util.py:6606-6618` (`resolve_remote_post_from_search`), `:6086`/`:6121` (microblog Announce miss), `app/relays/inbound.py:285-287` (relayed-object miss), `app/discovery/credits.py:295,369` (SERVER_NAME-only local check → `is_hosted_uri`), `app/community/util.py:242,319,517` (`retrieve_mods_and_backfill`: return at once for `community.is_local()`), `app/discovery/backfill.py:25-36`, `app/discovery/sync.py:110,161,182`, `app/discovery/filters.py:55` (`host_is_excluded` excludes hosted hosts), `app/shared/tasks/maintenance.py:1095` (instance chooser skips hosted domains).

**Interfaces:** Produces `local_object_for_url(url: str) -> Post | PostReply | User | Community | Feed | None` — None unless `is_hosted_uri(url)`. Matches, in order: exact `ap_id`/`ap_profile_id` (case-folded host, trailing slash dropped); then path forms `/post/<id>`, `/comment/<id>`, `/c/<name>/p/<id>[/<slug>]`, `/post/<id>/<slug>`, `/u/<name>`, `/c/<name>`, `/f/<name>` — names resolved on the URL's own domain (`domain_of_url(url).id` via C1's per-domain name lookup), ids by primary key. A hit on an object owned by another of our domains is still returned (the DB is shared; only the 404 rule for *serving* differs). Callers that got a hosted URL and a None result stop: no fetch.

- [ ] Tests: each path form on shop.example and on hell.cloud resolves to the right row, including a shop.example name that also exists on hell.cloud; `/post/5` asked on the wrong sister still returns post 5; an unknown id → None and no HTTP; a remote URL → None (caller fetches as before); search for `https://shop.example/c/den/p/5/slug` shows the post without HTTP; relayed Announce of a hosted object it doesn't have → dropped, no fetch; an empty local community added to a feed / imported in settings → no GET of its moderators, outbox or featured URLs; discovery backfill/sync of a hosted community → skipped; credits with a hosted href that is not a stored `ap_profile_id` → no `fetch_actor_document`.
- [ ] Commit: `feat: a URL on one of our domains is looked up, never fetched (C4)`.

### Task 4: A sister's handles resolve on the sister, and our own domains are always allowed

**Files:** `app/api/alpha/utils/misc.py:226-227` (`/c/den` → `!den@<the URL's domain>`), `app/api/alpha/utils/community.py:229`, `app/shared/feed.py:884` (storefront-blind `@SERVER_NAME` default → the acting/owning domain's name), `app/activitypub/actor.py:56-90` (`validate_remote_actor`) and `app/community/util.py:98` (`search_for_community`): a hosted host skips `instance_allowed`/`instance_banned`.

- [ ] Tests: API resolve of `https://shop.example/c/den` returns shop's den when hell.cloud also has a den; the two handle defaults name the sister on a shop.example request and are unchanged on hell.cloud; with `use_allowlist` on and an allowlist that lists neither domain, `find_actor_or_create('https://shop.example/u/alice')` returns alice and community search finds `den@shop.example`; a remote non-allowlisted host is still refused.
- [ ] Commit: `fix: a sister's names resolve on the sister, and our domains pass the allowlist (C4)`.

### Task 5: A sister-domain reply is stored directly and reaches remote followers from the right domain

**Files:** `tests/test_sister_domain_pseudo_federation.py` (end-to-end, the spec §5 required test). Production code changes only if a test fails.

- [ ] Tests (httpx captured, never real): a shop.example user replies to a hell.cloud post in a hell.cloud community with one remote follower instance → the reply row exists at once with an `ap_id` on shop.example; zero HTTP requests to any hosted domain; exactly one Announce to the remote follower's inbox with `actor` = the hell.cloud community, id on hell.cloud, `keyId` = community's `#main-key`, signature verifying with the community's key, and an inner `Create` whose actor and id are on shop.example; the hell.cloud post's author gets a reply notification without HTTP; the same for a vote and for a post (the post also reaches the shop user's piefed/mbin followers as an Announce and others as the user-signed Create, per `pages.py:410-497`). Mirror case: a hell.cloud user replies in a shop.example community → Announce from shop.example, inner Create on hell.cloud.
- [ ] Commit: `test: a sister domain's reply is stored directly and federated by the right domain (C4)`.

### Task 6: Deploy and verify (controller)

- [ ] After autodeploy: 0 new traceback kinds over 10 min in all app units; no `hosted domain, not sent` log rows on prod in the first hour (any row means a fan-out still aims at us — investigate); the `ChatMessage target is not local` / `empty uri` failure counts do not rise (the `empty uri` one should fall to 0); hell.cloud identity documents byte-identical to the C3 after-set (`~/backups/2026-10-24-identity/after`).
- [ ] Update DEPLOY_GUIDE §11 and the memory `onion-storefront-track`; note the live sister checks owed when the owner adds a second domain (Task 5's scenario over real HTTP to a remote follower).

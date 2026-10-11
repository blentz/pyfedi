# ActivityPub interop track C1: small interop fixes

Status: design approved in session (2026-10-11). Roadmap: `2026-10-10-ap-gaps-roadmap.md`, tier C.
Every item was re-checked against HEAD `945da950a`; the evidence is cited with current line numbers
(R = `app/activitypub/routes.py`, U = `app/activitypub/util.py`). Search by content if lines have moved.

## Scope

Tier C is split into separate specs. This spec, C1, covers the small fixes. These get their own specs later:

- C2: quote posts on input.
- C3: multi-language content, plus a self-hosted LibreTranslate container.
- C4: Create of Audio, Image, Document and Video as posts.
- C5: `likes`, `shares` and `liked` collections.

## Global constraints

- Follow the defect workflow:
  - one commit per item, or per sub-item where noted;
  - TDD, with a failing test first;
  - 100% line and branch coverage of new and changed lines
    (`tests/check_changed_line_coverage.py coverage.json <base> --branches app/`);
  - per-module floors kept (`tests/check_coverage_floors.py coverage.json coverage_floors.ini`).
- Tests run through `./run_tests.sh`. Gate tools are committed in `tests/`.
- No inline imports (the `tests/test_no_inline_imports.py` ratchet).
- Every remote fetch goes through the capped helpers: `get_request` or `signed_get_request` (1 MiB / 15 s), or
  `fetch_object_capped`. Never call `httpx_client` directly.
- No sleep or retry on a request or inbox path (D738/D775).
- Inbound handling must not trust the body over the verified signer. The signed actor is the only identity.
- AP cache policy table (D160): actor 300 s, content 120 s, collections 60 s, misses no-store. In secure mode,
  `secure_mode_cache_headers` still applies.

## Rulings (owner, 2026-10-11)

| Topic | Ruling |
|---|---|
| Person `followers`/`following` | Count only, like D175: advertised, real `totalItems`, empty items. |
| Undo of a Flag | Withdraw the report if it is still open. If moderators already acted, keep it and log the Undo. |
| Delete of a remote Group | Mark it deleted and keep its content: read-only, local members unsubscribed, content still readable. |
| String `endpoints` | Fetch it, only on the actor's own host, capped. On failure fall back to the actor's inbox. |
| Person outbox | Public posts and comments, as Create activities, paged. |

## Items

### 1. HTML discovery

Today:

- `app/templates/base.html:78-106` has `canonical`, `fediverse:creator`, RSS and oembed links, but no AP alternate.
- The dillo theme head (`app/templates/themes/dillo/base.html:20-32`) has none of these.
- Post pages already send the HTTP `Link` alternate header (`app/post/routes.py:456-460`, D1086).
- `fediverse:creator` is set only for article, link, poll, event and microblog posts (`app/post/routes.py:342-346`).

Change:

- A new render kwarg `ap_alternate` (the AP id URL) makes `base.html` and the dillo base emit
  `<link rel="alternate" type="application/activity+json" href="...">`.
  - It is passed by the post page, user profile (local and remote), community page and feed page.
  - For remote users and communities it is the actor's `ap_public_url`/`ap_profile_id`. For local ones it is
    `public_url()`.
- The user profile (`show_profile`, `app/user/routes.py`) and the community page (`app/community/routes.py:760`)
  add `Link: <id>; rel="alternate"; type="application/activity+json"` with `headers.add` and
  `Vary: Accept`, like the post page.
  - The user profile wraps its `render_template` in `make_response`.
- `fediverse:creator` is also set for plain Page posts whose author is local.
- No change to ETag caching. The link is the same for every viewer.

Tests: the alternate link and header on each page type, the dillo theme, a remote actor's URL, and a private or
local-only community (the alternate is still emitted; the AP side already refuses).

### 2. Inbound actor Update

Today:

- An Update whose object is a Person, Service or Application reaches the Create/Update arm (R:1466) and logs
  `Unacceptable type (create): Person` (R:1559).
- Profiles and keys refresh only daily.
- `refresh_user_profile(user_id, activity_json=None)` (U:783) already accepts a pushed document and updates the key
  (U:884-887).
- The Group Update path (R:1550) calls `refresh_community_profile(community.id, object)` without comparing the
  object id to the signer.

Change:

- An Update with object type Person, Service or Application calls `refresh_user_profile(user.id, object)` inline.
  The document is pushed, so there is no fetch. It is applied only when all of these hold:
  - `object['id']` equals the signed actor's `ap_profile_id`. Compare with the same normalisation used for actor
    ids elsewhere.
  - The actor is a known remote user. An Update for an unknown actor is logged and dropped; it does not create
    the actor.
  - The refusal is logged at the usual inbox log level with a reason. It is not an exception.
- The same `object['id'] == signer` check is added to the Group Update path. An Update about another Group is
  refused and logged.
- `refresh_user_profile` (and the community and feed refreshes, through `shared_inbox_of`) also updates `ap_inbox_url`
  and the shared inbox from the document. It keeps the old values when they are missing.
- The Castopod `Podcast` Service and the `bot` flag keep their existing handling (U:1439, `ensure_podcast_community`).
- The test pinning `Unacceptable type (create): Group` / `Tombstone` (`tests/test_inbox_dispatch_create_update.py`)
  is updated only where the behaviour changes.

Tests:

- a Person Update refreshes the name, summary and key;
- a Service Update keeps the bot and podcast behaviour;
- an Update with a mismatched id is refused, for both Person and Group;
- an unknown actor is dropped;
- a missing key keeps the old key;
- the inbox URL is refreshed.

### 3. Undo of EmojiReact, Flag, Add and Remove

Today: the Undo dispatch (R:2043-2254) handles Follow, Delete, Like/Dislike, Announce, Lock, Block and
ChooseAnswer. Anything else logs `Unmatched activity` (R:2253).

Change, one commit per inner type:

- **EmojiReact**
  - An Undo whose inner type is `EmojiReact` is handled like an Undo of a Like: `undo_vote` (U:4449) on the vote
    with that emoji.
  - `undo_vote` also rebuilds `Post.emoji_reactions` / `PostReply.emoji_reactions` (models.py:3953, :4599).
    Today it leaves the cache stale, which also affects an Undo of a Like.
- **Flag**
  - New nullable, indexed column `Report.ap_id` (String 255), with a migration. It is set by `process_report`
    (U:4505) from the Flag's `id` on new inbound reports.
  - An Undo of a Flag finds the report by `ap_id`. Failing that, it takes the newest report with the same reporter
    and suspect target.
  - If the report is open (`status` 0, new), it is deleted. Any open-report counts and notifications derived from
    it are corrected, using the same code paths moderators use to discard a report.
  - If the report is escalated, appealed, resolved or discarded (`status` 1-4), it is kept and the Undo is
    logged.
  - Only the reporter may undo: the signer must equal `report.reporter`.
- **Add/Remove**
  - An Undo of an Add runs the matching Remove logic, and an Undo of a Remove runs the matching Add logic. This
    covers sticky (`post.sticky`), moderators (`CommunityMember.is_moderator` with a modlog entry and the
    `cache.delete_memoized` calls at R:1789-1794) and feed items (`FeedItem`).
  - The authority checks are exactly those of the original Add/Remove handlers. Factor them into a shared
    predicate if they are inline.
  - An Undo whose target state already holds is a no-op that is logged.

Tests: each inner type, both outcomes of the Flag (open versus acted on), the reporter-only rule, the cache
rebuild, the authority refusal for Add/Remove, and the no-op.

### 4. String `endpoints`

Today: `shared_inbox_of` (U:731) returns None for a non-dict `endpoints`. Its callers (U:1481, U:1613, U:1891) fall
back to `inbox`.

Change:

- When `endpoints` is a string, `shared_inbox_of` fetches it with `fetch_object_capped` (or `get_request` with the
  default cap) and reads `sharedInbox` from the result. All of these must hold:
  - The URL's host equals the actor's host.
  - The result is a dict with a string `sharedInbox`.
  - That `sharedInbox` is on the same host as the actor.
- On any failure it returns None, and callers keep falling back to the actor's inbox.
- Refreshes (item 2) use the same function.
- No retry or sleep.

Tests: a same-host string is fetched and used, a cross-host string is not fetched, a cross-host `sharedInbox` in
the fetched document is ignored, a fetch failure falls back, and a non-dict result falls back.

### 5. Content type

Today:

- AP routes hard-code `application/activity+json` (R:481, 547, 595, 687 and more).
- `is_activitypub_request()` (`app/utils.py:2338`) accepts both forms by substring.
- Outgoing GETs send only `application/activity+json`. Signed GETs send `application/ld+json`
  (`app/activitypub/signature.py:775`).

Change:

- One helper, `ap_media_type()`, in the module beside `ap_content_headers`. It returns
  `application/ld+json; profile="https://www.w3.org/ns/activitystreams"` when the request's Accept lists that
  form, or plain `application/ld+json`, with a higher q-value than `application/activity+json`, or without
  `application/activity+json` at all. Otherwise it returns `application/activity+json`.
- Every AP route sets its content type through the helper.
- `Vary: Accept` is already sent and stays.
- The inline Accept tests at R:608, R:3248 and `app/main/routes.py:71` use `is_activitypub_request()`.
- `is_activitypub_request()` ignores any media type whose q-value is 0.
- Outgoing AP GETs send
  `Accept: application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"`.
  Use a single constant for it, including for signed GETs. WebFinger and nodeinfo fetches keep their own Accept.
- `secure_mode_cache_headers` keeps working, because `response.mimetype` drops parameters.
- Existing tests that pin the old Accept (`tests/test_activitypub_actor.py:1089`) are updated.

Tests:

- each Accept form gets the right content type: activity+json only, ld+json with profile, ld+json without
  profile, both with q-values, and q=0;
- an outgoing fetch sends both forms;
- a signed GET still verifies at the peer. Check whether `Accept` is among the signed headers; if it is, the
  signature is built over the new value.

### 6. Person `followers` and `following`

Today:

- The Person document (R:494-513) lists neither.
- `user_followers` (R:2548) returns every accepted remote follower URL, unpaged.
- There is no `following` route.

Change:

- The Person document gains `followers` (`user.followers_url()`) and `following` (`public_url() + '/following'`).
- `user_followers` returns `{"type": "OrderedCollection", "id": ..., "totalItems": <count>, "orderedItems": []}`.
  - The count is all accepted followers, local and remote.
  - This is the same shape as community followers (D175).
- A new `user_following` route returns the same shape. Its count is every accepted follow by the user: users
  followed plus communities joined. A community join is a Follow of a Group actor, so peers count it too.
- Both routes:
  - follow the existing user-route gates: deleted or banned users get 410 or 404 as the security track ruled;
  - honour secure mode;
  - use the collections cache policy (60 s);
  - redirect a non-AP request to the HTML profile (D177).

Tests: both keys in the Person document, the counts, empty items, the gates, the non-AP redirect, and that no
follower URL leaks.

### 7. Person outbox

Today: `user_outbox` (R:589) returns `paged_outbox(..., 0, lambda offset, limit: [])`, which is always empty.

Change:

- `user_outbox` returns the user's own posts and comments as Create activities, newest first, through
  `paged_outbox` (50 per page).
- Only content a logged-out viewer could see is included. All of these must hold:
  - not deleted;
  - not followers-only;
  - not in a private or local-only community;
  - not a DM or conversation message;
  - not from a banned or deleted author.
  Reuse `visible_to_clause(model, None)` (`app/visibility.py:125`) plus the existing public and federation
  filters.
- `totalItems` is the real count of the same query.
- Each activity is built with the existing builders (`post_to_page` / `comment_model_to_json`) inside the same
  Create envelope the Group outbox uses. The Create's id and shape are the ones the Group outbox
  already uses.
- Merging posts and comments by `posted_at` needs a query that stays bounded per page: union with LIMIT/OFFSET, or
  two bounded queries merged. No full-table scans per request. Use indexes on author and posted_at; add one in a
  migration if `EXPLAIN` shows none.
- Cache policy and gates are as for item 6.

Tests:

- ordering across posts and comments;
- paging, including the first page inline;
- `totalItems`;
- each excluded class: followers-only, private community, local-only, deleted, DM;
- a remote user's outbox is not served;
- the query count per page is bounded.

### 8. Array-valued `type`

Today:

- The inbox dispatch compares `type` by equality, so an array drops the activity silently.
- An array object type raises TypeError at R:1559 (`'Unacceptable type (create): ' + object_type`), which fails
  the Celery task.

Change:

- At the inbox entry, before dedupe keys and dispatch, normalise `type` on the activity, its `object` and any
  Announce inner object.
  - An array becomes its first element that is a known AS2 or extension type the code dispatches on.
  - Failing that, it becomes its first string element.
  - With no string element, the activity is refused and logged.
- The normalisation works on a copy that dispatch uses. The stored or logged original JSON is unchanged, so LD
  signature checks still see the original.
- The R:1559 log line is made safe for any type value.

Tests: an array activity type is dispatched, an array object type is dispatched, an unknown array falls through
and is logged without an exception, an array of non-strings is refused, and LD verification still uses the
original.

### 9. WebFinger reverse check and JSON host-meta

Today:

- `actor_json_to_model` (U:1403-1437) checks only that the actor's id host equals the WebFinger server.
- host-meta is XML only (R:263-270).

Change:

- After an actor is resolved from a handle `name@server` (`fetch_actor_from_webfinger`, actor.py:262), take the
  canonical handle `preferredUsername@<actor id host>`.
  - If it equals the looked-up handle (case-insensitive), store as today.
  - Otherwise, do one capped WebFinger lookup of the canonical handle. If that resolves to the same actor id,
    store the actor under the canonical handle, not the looked-up one.
  - If it does not resolve to the same actor id, refuse the actor and log it.
  - Do this once per resolution, with no retry.
- A new route `/.well-known/host-meta.json` returns the JRD equivalent of the XML host-meta: the `lrdd` link with
  a `template`, content type `application/jrd+json`, cached like the XML route (`@cache.cached(timeout=600)`).

Tests: the matching handle, a mismatch fixed by the reverse lookup, a mismatch refused, a case-insensitive match,
and the host-meta.json body and headers.

### 10. Delete of a remote Group

Today:

- Nothing writes `Community.ap_deleted_at` (models.py:1312).
- A self-Delete from a Group logs `Does not exist here` (R:795-803) or `Unexpected activity from Group` (R:1058).

Change:

- A Delete whose actor and object are the same remote Group is accepted only after verifying it at the origin:
  - refetch the Group's id with the capped fetch;
  - only a 410 or a Tombstone counts as deleted;
  - a 404 does not, consistent with the ruling that a 404 does not delete private objects;
  - an unreachable origin means not verified, logged, no change.
  - The verification reuses `_origin_refusal_of_delete` or the same shape.
- A Delete of a Group by any other actor is refused and logged. A Group-host admin Delete is not in this scope.
- On a verified Delete:
  - set `ap_deleted_at`;
  - unsubscribe local members (`CommunityMember` rows), without sending Undo Follow to the deleted Group;
  - clear the community's memoized caches.
- A community with `ap_deleted_at` set is read-only. One predicate, e.g. `Community.is_gone()`, is used at every
  write path:
  - creating a post, reply or vote there (web, API, inbox) is refused with the existing "community not
    available" message;
  - joins are refused;
  - outbound delivery to it is skipped;
  - existing posts and comments stay readable and searchable;
  - the community page shows a "this community was deleted on its home server" notice.
- WebFinger and discovery already read `ap_deleted_at` (R:201, `app/discovery/sync.py`,
  `app/relays/inbound.py:163`). Keep them consistent.
- No undelete path in this spec. A later Update or Create from the same Group is ignored while `ap_deleted_at` is
  set, and logged.

Tests:

- a verified Delete marks the community and unsubscribes;
- a 404 does not mark it, and neither does an unreachable origin;
- a Delete by another actor is refused;
- each write path is refused: web, API and inbox for post, reply, vote and join;
- content stays readable;
- outbound delivery is skipped;
- the notice renders.

## Order

Items are independent. Suggested order, cheapest and highest-value first: 8 (crash), 5, 2, 4, 3, 10, 6, 7, 1, 9.
Item 4 must land before item 2's inbox refresh relies on it, or item 2 uses `shared_inbox_of` as it stands.

## Out of scope

- Group-host admin Delete of a remote Group, and community undelete.
- Full follower and following lists (ruled count-only).
- Quote posts, multi-language, media-type posts, and the likes/shares/liked collections (C2-C5).

## Amendment A: rulings from plan review (2026-10-11)

These supersede the item text above where they differ.

| Topic | Ruling |
|---|---|
| Item 10, who may delete a remote Group | Accept a Delete of a remote Group from the Group itself, one of its known moderators, or an admin on the Group's host (the rule the security track uses for Group-host admins). The origin refetch must still answer 410 or a Tombstone. A Delete from anyone else is refused and logged. Lemmy sends community deletions with a moderator as the actor. |
| Item 9, scope of the reverse check | One shared helper, used by every WebFinger resolver: `fetch_actor_from_webfinger`'s caller, `search_for_user` (`app/user/utils.py`), `search_for_community` (`app/community/util.py`) and `search_for_feed` (`app/feed/util.py`). |
| Item 1, `fediverse:creator` | Every post type by a local author gets the tag, image posts included. The test pinning image posts as excluded (`tests/test_post_show.py`) is flipped. |
| Item 2, unknown actor | The inbox's HTTP gate still creates an unknown signer, because signature verification needs its key. Dispatch looks the signer up without creating it, and logs and drops a miss. |
| Item 2, Group Update | The object id must name the community the activity is processed for. For a remote Group, the Group must also be the signer, either sending the Update itself or announcing it. A local community's moderator edit stays allowed. |
| Item 3, Flag withdrawal | There is no reusable discard path. A new `withdraw_report` deletes the report, lowers the suspect's report counter (floor 0, leaving -1 untouched), deletes the unread report notifications it raised, and recounts unread counts. "Open" means `status == REPORT_STATE_NEW`. |

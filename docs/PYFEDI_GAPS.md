# pyfedi ActivityPub gaps

Read-only audit of pyfedi at commit `02dc4c10f` (2026-10-10). It compares the code against three sources:

- the W3C ActivityPub Recommendation,
- the Activity Streams 2.0 (AS2) vocabulary,
- the SWICG task-force reports (https://github.com/swicg, https://www.w3.org/community/socialcg/, https://activitypub.rocks/).

Items marked ✓ were checked by hand against the source. All other items come from automated code audits and should be confirmed before you act on them.

Abbreviations: `R` = `app/activitypub/routes.py`, `U` = `app/activitypub/util.py`.

## 1. Core ActivityPub (W3C Recommendation)

### Client-to-server API (§6): not implemented ✓

- Every inbox route is POST-only (R:827, 1029, 1036, 1041, 1046, 3850). The owner cannot read their inbox over ActivityPub.
- Outbox routes are GET-only. Clients cannot POST activities to them.
- None of the following exist: the OAuth authorization server, the `oauthAuthorizationEndpoint` and `oauthTokenEndpoint` endpoints, `uploadMedia`, `proxyUrl`, and `provideClientKey`/`signClientKey`.
- Clients use the Lemmy-style REST API at `/api/alpha` instead, with HS256 JWT bearer tokens.

### Actors and collections (§4–5)

| Item | Status | Evidence / note |
|---|---|---|
| Person outbox | Served ✓ | `user_outbox` (R:739) returns a paged collection of Create activities for the local user's public posts and comments (`user_outbox_rows`); nothing in a private, local-only or banned community, nothing local-only, deleted or under review |
| Person `followers` / `following` | Advertised, count-only | The Person document (R:570-572 as of 2026-10-28) lists both, and `user_followers` / `user_following` serve an OrderedCollection with `totalItems` and no items. The aptest `followers` and `following` checks pass for a Person. The instance actor (Application) advertises neither. |
| `following` on Group | Served, count-only | The Group document advertises it (`app/activitypub/util.py:1105`) and `community_following` (R:2712) answers a collection with `totalItems` 0, since a community follows nothing. Feeds (`/f/`) have it too (R:3891). |
| `liked` on Person | Served, count-only | Advertised on the Person document (R:572) and served by `user_liked` (R:2747) as an OrderedCollection with `totalItems`. The aptest `liked` check passes for a Person. |
| `liked` on Group, Feed and instance actor; `streams` | Missing | The aptest `liked` check is inapplicable for them. |
| `likes`, `dislikes`, `shares` on objects | Served, count-only | A local post embeds `likes`, `dislikes` and `shares` (`post_count_collections`, U:396) and serves them at `post_count_collection_ap` (R:3029); a local comment has `likes` and `dislikes` (U:405, `comment_count_collection_ap` R:2797). Totals only, no items. |
| Group followers | Partial | Returns `totalItems` only, with empty items (by design, D175) |
| Group outbox | Full | Paged OrderedCollection |
| Instance actor outbox | Served, empty | `/actor` advertises `/actor/outbox`, which `instance_actor_outbox` (`app/main/routes.py:1383`) serves as an empty OrderedCollection; the aptest outbox check passes. The site Application document at `/` advertises `/site_outbox`, which `site_outbox` (`app/main/routes.py`) serves the same way, empty and per domain. |
| `endpoints` | Partial | Only `sharedInbox` |

### Fetching objects (§3)

- Private messages are fetchable only by a party to them. `private_message_ap` (R:2896) serves `/private_message/<id>` to the recipient, or to a signer on the recipient's host; anyone else gets 404.
- Followers-only posts and comments return 404 to an unsigned fetch, but a signed fetch by an entitled signer is served (`followers_entitled`, R:2848; `post_ap_refusal` R:2935, `comment_ap_refusal` R:2756). Entitled means an accepted follower or a signer on a follower's host, or a user it answers or mentions.
- Deleted posts, comments and local users return 410 with a `Tombstone`. A post's `formerType` comes from `post_to_page` (`former_type_of_post`, R:2810), a comment's is `Note` (R:2766), and a deleted local user's is `Person` or `Service` (R:543). A banned user, a banned or unknown community and a feed return a plain 404 (`user_profile` R:516, `community_profile` R:762, `feed_profile` R:3761).
- `/activities/<type>/<id>` (`activities_json`, R:3072) serves a logged activity only to whoever its addressing entitles: anyone if it is public, else an entitled signer. A refusal is the same 404 as a miss.
- Responses use `application/activity+json`, or the `application/ld+json; profile="https://www.w3.org/ns/activitystreams"` form when the Accept header prefers it (`ap_media_type`, R:2867). Outgoing fetches send both `application/activity+json` and `application/ld+json` (`AP_FETCH_ACCEPT`, `app/constants.py:216`).
- ✓ The community outbox (`community_outbox`, R:2581) refuses a `local_only` or `private` community with 403, as the community actor document does (R:2590-2591).

### Delivery (§7)

- `bto`/`bcc` are never produced ✓. A peer's are removed, at every depth of `object`, before a local community re-Announces its activity and before `/activities/<type>/<id>` serves one (`without_blind_addressing`, `app/activitypub/entitlement.py`). The stored copy keeps them, since who it entitles is read from there (`ADDRESS_FIELDS`).
- Recipients come from database relations (instance followers, mentions, parent authors, community followers), not from expanding the `to`/`cc` addresses, which are built from the same relations. A Like, Dislike or Undo of one also goes to the author's own inbox (commit 45d948185; `follow_answer_inbox()`, `app/shared/tasks/likes.py`), but only when the author is remote, the content is in a local community and the content is open (public or unlisted), and the author's instance is online and not banned and the voter has not blocked it. The community must not be local-only or private, and the voter not banned from it. Otherwise a vote reaches only the instances following the community.
- Bulk fan-out goes to each instance's inbox: its Application actor's, or a guessed `https://domain/inbox` (U:3567-3626). A send aimed at one actor (Follow and its Undo, a post, reply, vote or moderation action sent to a community, a chat message) goes to that actor's stored `ap_inbox_url`, which is its shared inbox when it advertises one (`shared_inbox_of`, U:1237). The answer to a Follow and a vote to a remote author go to the actor's own `inbox` (`follow_answer_inbox`, `app/models.py:2618`). Flag reports go to the instance inbox.
- There is no general inbox forwarding (§7.1.2). Only a local Group re-Announces to its followers. Replies to a local user's followers-only post are not forwarded to that user's followers.
- Duplicate detection lasts 24 hours ✓. `claim_activity` (U:945) sets a Redis key scoped to the actor's host with a 86400-second TTL (`ACTIVITY_DEDUPE_SECONDS`, U:936), after the signature has verified (R:985). After that, a replayed activity is caught only if the handler for that activity is idempotent.
- Retries: a POST that fails in transport or with a 429 or 5xx is queued in SendQueue and retried after `60 * 2**retries` seconds, capped at 15360 (about 4h17m), up to 40 times (`app/activitypub/signature.py:230-238`); entries older than 7 days are purged. Deliveries published through `piefed_notifs` (votes, when `NOTIF_SERVER` is set) are not retried.

### Handling received activities (§7.2–7.12)

- **Create:** Page, Article, Link, Note, Question, Event, Audio, Document, Image and Video objects are accepted (`NEW_CONTENT_TYPES`, U:347). Any other type is rejected as "Unacceptable type". An Update of a media object pyfedi does not hold is ignored (PeerTube sends many).
- **Update:**
  - An Update of a Person, Service, Application or Podcast sent by the actor itself is applied (`ACTOR_UPDATE_TYPES`, U:7141). One that is Announced or accepted only on an LD signature is refused. A signature that fails under the stored key triggers one rate-limited refetch of the key (`verify_refreshing_key`, `app/activitypub/signature.py:500`).
  - Update merges fields one by one instead of replacing the whole object (`update_post_reply_from_activity` U:4709, `update_post_from_activity` U:4880).
- **Delete:** a Delete of a remote Group is applied only when it comes from the Group, one of its moderators or an admin of its host (`may_delete_group`, U:7065).
- **Block:** Block is handled only as a site or community ban. A Block from one actor to another is not modelled.
- **Undo:** Undo is handled for Follow, Delete, Like, Dislike, EmojiReact, Announce, Lock, Block, ChooseAnswer, Add, Remove and Flag. Any other inner type has no handler.
- **Move:** Move means moving a post between communities. An account Move is not supported.
- **Announce:** an inner activity whose id is on the Group's own host is trusted on the Group's signature. One from another host must name an actor on that host; an inner Create or Update is then refetched from its origin, and an inner Delete needs the origin to answer 404 or 410. Under an LD-signed Announce, every inner Create, Update and Delete is checked that way (`verify_announced_inner`, U:7180).
- **AS2 activity types handled in neither direction:** Arrive, Ignore, Invite, Join, Leave, Listen, Offer, Read, TentativeAccept, TentativeReject, Travel, View. `Question` is handled only as a poll or post object, not as an activity.
- **AS2 object types not supported:** Place, Profile, Relationship.

### JSON-LD and properties

- Payloads are processed as plain JSON with fixed key names. Prefixed terms such as `as:content` and terms aliased by a custom `@context` are not recognised. pyld is used only to normalize documents for LD-signature checks.
- ✓ An array-valued `type` on the activity, its object or an Announce's inner object is reduced to one type for dispatch, and refused if it names more than one type pyfedi dispatches on (`normalise_activity_types`, U:1423).
- `contentMap`: pyfedi emits a single language. On input it reads every `contentMap`, `nameMap` and `summaryMap` entry as a per-language variant; the default is the key whose text matches `content`, else the `language` property, else the first key (`language_variants_from`, U:304).
- `generator` and `preview` are ignored. For `icon`, pyfedi takes the last entry (`image_url_from(..., prefer_last=True)`, `app/models.py:173`). `location` is kept only on Events. Incoming `replies` is read only for poll vote counts.

### Verification

- HTTP Signatures: inbound, both draft-cavage-12 (`rsa-sha256` or `hs2019`, RSA keys) and RFC 9421 (`rsa-v1_5-sha256`, `rsa-pss-sha512`, and ed25519 keys) are verified (`app/activitypub/signature_rfc9421.py`). Outbound, pyfedi signs cavage with `rsa-sha256` or RFC 9421 with `rsa-v1_5-sha256`, in the format it remembers for the peer, and on a 401 retries once in the other (`double_knock`, `app/activitypub/signature.py:463`). ed25519 is verify-only.
- LD Signatures (`RsaSignature2017`) are used as a fallback when the HTTP signature check fails, or when its keyId is on another host than the actor. Content accepted on one is refetched from its origin rather than taken from the body.
- FEP-8b32 object-integrity proofs are not supported.

## 2. SWICG task-force reports

All of these reports are still drafts or living documents. The test-suite task force has no tests or results yet.

### Account migration (data-portability report): missing ✓

- pyfedi never reads or writes `movedTo`.
- `alsoKnownAs` is read only for discovery credits (`app/discovery/credits.py:173-177`) and is not stored.
- Export and import use a PieFed-specific settings format, not the report's following-list CSV. LOLA is not supported.

### HTTP Signatures report

- Only draft-cavage is supported ✓. There is no RFC 9421, no `Signature-Input` and no `Content-Digest`.
- There is no double-knocking: pyfedi never falls back between the cavage and RFC 9421 formats, in either direction.
- `verify_request` (`app/activitypub/signature.py:441`) checks whatever headers the sender chose to sign ✓. It does not require `(request-target)`, `host` or `digest` to be among them. The body digest is compared separately, but nothing requires the digest header to be signed. The inbox also passes `skip_date=True` (R:801).
- When a signature fails, the inbox returns 400 straight away ✓ (R:819-822). It does not refetch the sender's key and retry, so remote key rotation breaks delivery until the daily actor refresh.
- `keyId` is ignored. The request is verified against the key of the body's `actor`.
- Fetches are never required to be signed. Responses send `Vary: Accept`, without `Signature`.

### HTML discovery report: missing ✓

- `app/templates/base.html` has no `<link rel="alternate" type="application/activity+json">` tag.
- An HTTP `Link` header for ActivityPub is sent on post pages only (`app/post/routes.py:456`), not on user or community pages.
- There is no `fediverse:creator` meta tag.

### WebFinger report

- Mostly supported, including the FEP-3b86 links. FediTest's WebFinger server tests pass (no baseline entry).
- A request with no `resource` parameter answers 400 (RFC 7033 4.2). A well-formed resource of a scheme this server does not serve (`mailto:`, `foo://`) answers 404, not 400; only a resource with no scheme is malformed.
- Reverse discovery is not checked: pyfedi does not verify that the actor links back to the handle it was found by.
- host-meta is served only as XRD, with no JSON variant.

### Trust and safety report

- `Flag` works in both directions. Flags have no categories or evidence, but the report has not defined those yet.

### Groups report (threadiverse)

- FEP-1b12, FEP-7888 `context`, moderators and `featured` are supported.
- The draft's `Member` records and Join/Leave membership model are not. Membership is based on Follow.

### Remix (quote posts)

- FEP-044f (QuoteRequest and `interactionPolicy.canQuote`) is supported.
- The legacy quote properties `quoteUrl`, `_misskey_quote` and `quoteUri` are not read on input ✓.

### Other reports

- E2EE / MLS: not supported.
- Geosocial: only an Event's `location`. `Place` is not supported.
- Handles: only WebFinger handles.

### External conformance suites

`interop/run.sh conformance` runs three third-party suites against the interop stack and diffs every non-pass against `interop/conformance/baseline.json` (see `interop/README.md`, "Conformance suites"). Each entry there is an expected failure, error or skip, and each carries the reason below. A test that is not in the table passes. Rows whose reason starts `upstream:` are bugs in FediTest's Mastodon node driver or in the test itself, cited by file and line at the pinned feditest-tests-fediverse `cdbb467` and feditest `41cfd61`; PieFed's behaviour is correct there. The other rows are gaps in this document: each points at its §1 section.

| Test id | Outcome | Reason |
|---|---|---|
| `aptest/alice/create-then-update-modifies-object-checked-by-get` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); the outbox POST answers 405 |
| `aptest/alice/inbox-must-be-an-orderedcollection` | inapplicable | Inapplicable: every inbox route is POST-only and answers GET with 405 (404 on a feed); PYFEDI_GAPS §1 Client-to-server API (§6) |
| `aptest/alice/likes-collection-must-be-a-collection` | inapplicable | Inapplicable: no `likes` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `likes`, `shares` missing |
| `aptest/alice/outbox-post-must-accept-non-activity-object` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); the outbox answers POST with 405 |
| `aptest/alice/outbox-post-server-adds-to-outbox-collection-checked-by-outbox-get` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/alice/outbox-post-servers-must-return-a-201-created-http-code` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6) |
| `aptest/alice/outbox-wraps-object-with-create-checked-using-get-location` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/alice/post-outbox-server-overwrites-id-property` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/alice/shares-collection-must-be-a-collection` | inapplicable | Inapplicable: no `shares` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `likes`, `shares` missing |
| `aptest/conf-feed/actor-objects-must-have-inbox-outbox-properties` | inapplicable | Inapplicable: the test does not recognise the PieFed `Feed` actor type; `isActorObject` accepts only Application, Group, Organization, Person and Service (activitypub-testing@0.16.1 src/activitypub-tests/actor-objects-must-have-inbox-outbox-properties/actor-objects-must-have-inbox-outbox-properties.js:122 and :131-139) and answers inapplicable at :80-85; a limit of the test, not a PieFed defect |
| `aptest/conf-feed/create-then-update-modifies-object-checked-by-get` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); the outbox POST answers 405 |
| `aptest/conf-feed/inbox-must-be-an-orderedcollection` | inapplicable | Inapplicable: every inbox route is POST-only and answers GET with 405 (404 on a feed); PYFEDI_GAPS §1 Client-to-server API (§6) |
| `aptest/conf-feed/liked-collection-must-be-a-collection` | inapplicable | Inapplicable: no `liked` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `liked` on Group, Feed and instance actor; `streams` missing |
| `aptest/conf-feed/likes-collection-must-be-a-collection` | inapplicable | Inapplicable: no `likes` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `likes`, `shares` missing |
| `aptest/conf-feed/outbox-post-must-accept-non-activity-object` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); the outbox answers POST with 405 |
| `aptest/conf-feed/outbox-post-server-adds-to-outbox-collection-checked-by-outbox-get` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/conf-feed/outbox-post-servers-must-return-a-201-created-http-code` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6) |
| `aptest/conf-feed/outbox-wraps-object-with-create-checked-using-get-location` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/conf-feed/post-outbox-server-overwrites-id-property` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/conf-feed/shares-collection-must-be-a-collection` | inapplicable | Inapplicable: no `shares` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `likes`, `shares` missing |
| `aptest/instance/create-then-update-modifies-object-checked-by-get` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); the outbox POST answers 405 |
| `aptest/instance/followers-collection-must-be-a-collection` | inapplicable | Inapplicable: the instance actor advertises no `followers`; PYFEDI_GAPS §1 Actors and collections (§4-5) |
| `aptest/instance/following-collection-must-be-a-collection` | inapplicable | Inapplicable: the instance actor advertises no `following`; PYFEDI_GAPS §1 Actors and collections (§4-5) |
| `aptest/instance/inbox-must-be-an-orderedcollection` | inapplicable | Inapplicable: every inbox route is POST-only and answers GET with 405 (404 on a feed); PYFEDI_GAPS §1 Client-to-server API (§6) |
| `aptest/instance/liked-collection-must-be-a-collection` | inapplicable | Inapplicable: no `liked` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `liked` on Group, Feed and instance actor; `streams` missing |
| `aptest/instance/likes-collection-must-be-a-collection` | inapplicable | Inapplicable: no `likes` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `likes`, `shares` missing |
| `aptest/instance/outbox-post-must-accept-non-activity-object` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); the outbox answers POST with 405 |
| `aptest/instance/outbox-post-server-adds-to-outbox-collection-checked-by-outbox-get` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/instance/outbox-post-servers-must-return-a-201-created-http-code` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6) |
| `aptest/instance/outbox-wraps-object-with-create-checked-using-get-location` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/instance/post-outbox-server-overwrites-id-property` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/instance/shares-collection-must-be-a-collection` | inapplicable | Inapplicable: no `shares` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `likes`, `shares` missing |
| `aptest/microblogs/create-then-update-modifies-object-checked-by-get` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); the outbox POST answers 405 |
| `aptest/microblogs/inbox-must-be-an-orderedcollection` | inapplicable | Inapplicable: every inbox route is POST-only and answers GET with 405 (404 on a feed); PYFEDI_GAPS §1 Client-to-server API (§6) |
| `aptest/microblogs/liked-collection-must-be-a-collection` | inapplicable | Inapplicable: no `liked` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `liked` on Group, Feed and instance actor; `streams` missing |
| `aptest/microblogs/likes-collection-must-be-a-collection` | inapplicable | Inapplicable: no `likes` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `likes`, `shares` missing |
| `aptest/microblogs/outbox-post-must-accept-non-activity-object` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); the outbox answers POST with 405 |
| `aptest/microblogs/outbox-post-server-adds-to-outbox-collection-checked-by-outbox-get` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/microblogs/outbox-post-servers-must-return-a-201-created-http-code` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6) |
| `aptest/microblogs/outbox-wraps-object-with-create-checked-using-get-location` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/microblogs/post-outbox-server-overwrites-id-property` | inapplicable | C2S not implemented: PYFEDI_GAPS §1 Client-to-server API (§6); no outbox POST, so no Location header |
| `aptest/microblogs/shares-collection-must-be-a-collection` | inapplicable | Inapplicable: no `shares` property; PYFEDI_GAPS §1 Actors and collections (§4-5) `likes`, `shares` missing |
| `system2-mastodon-to-piefed/system2.follow_receive::FollowReceiveTest` | error | upstream: follow_receive.py:81 asks the Mastodon sender node whether the PieFed receiver account follows, and the stock MastodonSaasNodeDriver raises "failed to find account" for an account on another node; test bug, PieFed behaviour correct (feditest-tests-fediverse cdbb467, feditest 41cfd61) |
| `system2-mastodon-to-piefed/system2.reply_single_hop::ReplyTest` | failed | upstream: stock MastodonSaasNodeDriver returns note HTML where the test compares plain text (reply_single_hop.py:76 equal_to between the two nodes and :79 equal_to the plain reply cannot both hold with one Mastodon side); the reply is imported and delivered, PieFed behaviour correct (feditest-tests-fediverse cdbb467, feditest 41cfd61) |
| `system2-piefed-to-mastodon/system2.direct_addressing::SendNoteToTest` | failed | upstream: stock MastodonSaasNodeDriver returns note HTML (<p>..</p> plus the mention) where the test compares plain text (direct_addressing.py:56 equal_to); unsatisfiable for any Mastodon receiver, PieFed behaviour correct (feditest-tests-fediverse cdbb467, feditest 41cfd61) |
| `system2-piefed-to-mastodon/system2.reply_single_hop::ReplyTest` | failed | upstream: stock MastodonSaasNodeDriver returns note HTML where the test compares plain text (reply_single_hop.py:76 equal_to between the two nodes and :79 equal_to the plain reply cannot both hold with one Mastodon side); PieFed behaviour correct (feditest-tests-fediverse cdbb467, feditest 41cfd61) |

## 3. Bugs found during the audit

1. **A pending follow request can never be withdrawn ✓.** The handler for `Undo Follow` aimed at a User filters on `is_accepted=True` (R:1993-1994). An Undo of a follow request that has not been accepted yet therefore finds nothing to delete, and the request stays.
2. **Some remote actors cannot be created ✓.** U:1443 (and, per the audit, U:1578 and U:1856) reads `activity_json['endpoints']['sharedInbox']` whenever `endpoints` is present. The spec allows `endpoints` to lack `sharedInbox`, or to be a URI string. For those actors this raises KeyError or TypeError, and the actor is never created.

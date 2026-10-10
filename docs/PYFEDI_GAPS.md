# pyfedi ActivityPub gaps

Read-only audit of pyfedi at commit `02dc4c10f` (2026-10-10). It compares the code against three sources:

- the W3C ActivityPub Recommendation,
- the Activity Streams 2.0 (AS2) vocabulary,
- the SWICG task-force reports (https://github.com/swicg, https://www.w3.org/community/socialcg/, https://activitypub.rocks/).

Items marked ✓ were checked by hand against the source. All other items come from automated code audits and should be confirmed before you act on them.

Abbreviations: `R` = `app/activitypub/routes.py`, `U` = `app/activitypub/util.py`.

## 1. Core ActivityPub (W3C Recommendation)

### Client-to-server API (§6): not implemented ✓

- Every inbox route is POST-only (R:706, 856, 863, 868, 873, 3104). The owner cannot read their inbox over ActivityPub.
- Outbox routes are GET-only. Clients cannot POST activities to them.
- None of the following exist: the OAuth authorization server, the `oauthAuthorizationEndpoint` and `oauthTokenEndpoint` endpoints, `uploadMedia`, `proxyUrl`, and `provideClientKey`/`signClientKey`.
- Clients use the Lemmy-style REST API at `/api/alpha` instead, with HS256 JWT bearer tokens.

### Actors and collections (§4–5)

| Item | Status | Evidence / note |
|---|---|---|
| Person outbox | Empty ✓ | R:578 returns a paged collection built from `lambda offset, limit: []` |
| Person `followers` / `following` | Not advertised ✓ | The Person document (R:473-494) lists neither. A `followers` route exists (R:2409), but it is not paged and is not an OrderedCollection. |
| `following` on Group | Missing | Only feeds (`/f/`) have it (R:3046) |
| `liked`, `streams` | Missing | |
| `likes`, `shares` on objects | Missing | `post_to_page` emits only `replies` |
| Group followers | Partial | Returns `totalItems` only, with empty items (by design, D175) |
| Group outbox | Full | Paged OrderedCollection |
| Instance actor outbox | Broken | `/site_outbox` and `/actor/outbox` are advertised but have no route, so they return 404 |
| `endpoints` | Partial | Only `sharedInbox` |

### Fetching objects (§3)

- Private messages cannot be fetched ✓. Their ids are `/private_message/<id>`, but no route serves that path.
- Followers-only content returns 404 even to a follower who sends a signed fetch, so authorized fetch is not possible.
- Only posts and comments return 410 with a `Tombstone`, and `formerType` is hard-coded to `Page`/`Note` (R:2474-2479). Deleted or banned actors and communities return a plain 404.
- `/activities/<type>/<id>` (R:2612) serves logged activities with no visibility check.
- Responses always use the content type `application/activity+json`, never the `application/ld+json; profile="https://www.w3.org/ns/activitystreams"` form. Most outgoing fetches send only `application/activity+json` as their Accept header.
- Uncertain: the community outbox (R:2297) does not check `private`/`local_only`, but the community actor document does.

### Delivery (§7)

- `bto`/`bcc` are not supported ✓. pyfedi never produces, strips or honours them.
- Recipients come from database relations (instance followers, mentions, user followers), not from expanding the `to`/`cc` addresses.
- Bulk delivery always goes to each instance's shared inbox. The shared inbox comes from the instance's Application actor, or falls back to a guessed `https://domain/inbox`. Per-actor inboxes are not used.
- There is no general inbox forwarding (§7.1.2). Only a local Group re-Announces to its followers. Replies to a local user's followers-only post are not forwarded to that user's followers.
- Duplicate detection lasts only 90 seconds ✓. It is a Redis key with a 90-second TTL (R:762). After that, a replayed activity is caught only if the handler for that activity is idempotent.
- Retries: SendQueue retries with exponential backoff, from 1 minute up to 4 hours. Votes sent via `piefed_notifs` are not retried.

### Handling received activities (§7.2–7.12)

- **Create:** Page, Article, Link, Note, Question and Event objects are accepted. Audio, Image and Document are rejected as "Unacceptable type". Video is handled only as an Update that refreshes the score of a known PeerTube post.
- **Update:**
  - Update of a Person or Service is not handled. Remote profiles and keys are refreshed only by a daily refetch.
  - Update merges fields one by one instead of replacing the whole object (U:3833-3870).
- **Delete:** Delete of a remote Group is not handled.
- **Block:** Block is handled only as a site or community ban. A Block from one actor to another is not modelled.
- **Undo:** Undo of EmojiReact, Flag, Add and Remove has no handler.
- **Move:** Move means moving a post between communities. An account Move is not supported.
- **Announce:** pyfedi trusts the inner object on the Group's signature. Domain matching applies only to Creates that arrive without an Announce wrapper.
- **AS2 activity types handled in neither direction:** Arrive, Ignore, Invite, Join, Leave, Listen, Offer, Read, TentativeAccept, TentativeReject, Travel, View. `Question` is handled only as a poll object, not as an activity.
- **AS2 object types not supported:** Place, Profile, Relationship. Image and Audio are supported only as attachments.

### JSON-LD and properties

- Payloads are processed as plain JSON with fixed key names. Prefixed terms such as `as:content` and terms aliased by a custom `@context` are not recognised. pyld is used only to normalize documents for LD-signature checks.
- An array-valued `type` is probably not handled.
- `contentMap`: pyfedi emits a single language, and on input uses only the first key's language. `nameMap` and `summaryMap` are ignored.
- `generator` and `preview` are ignored. For `icon`, pyfedi takes `icon[-1]`. `location` is kept only on Events. Incoming `replies` is read only for poll vote counts.

### Verification

- HTTP Signatures support only draft-cavage-12, with the `rsa-sha256` or `hs2019` algorithm (RSA keys only).
- LD Signatures (`RsaSignature2017`) are used as a fallback when the HTTP signature check fails.
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

- Mostly supported, including the FEP-3b86 links.
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

## 3. Bugs found during the audit

1. **A pending follow request can never be withdrawn ✓.** The handler for `Undo Follow` aimed at a User filters on `is_accepted=True` (R:1993-1994). An Undo of a follow request that has not been accepted yet therefore finds nothing to delete, and the request stays.
2. **Some remote actors cannot be created ✓.** U:1443 (and, per the audit, U:1578 and U:1856) reads `activity_json['endpoints']['sharedInbox']` whenever `endpoints` is present. The spec allows `endpoints` to lack `sharedInbox`, or to be a URI string. For those actors this raises KeyError or TypeError, and the actor is never created.

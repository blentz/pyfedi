# ActivityPub gaps roadmap

Status: approved in session (2026-10-10). Source audit: `docs/PYFEDI_GAPS.md` (written at `02dc4c10f`).
Items were re-checked against HEAD `914cd4c00`. "Verified" means checked against the code at that commit;
"unverified" items must be confirmed by the sub-project that takes them before any change.

## Priority

The owner chose **security first**. Sub-projects run in this order:

1. **Security track:** tiers A, B and the security half of D. Detailed in
   `2026-10-10-ap-security-track-design.md`.
2. **Interop track (tier C):** small, independent interop fixes. This needs its own spec.
3. **Larger subsystems (tier D remainder):** one spec each, in the order listed below.
4. **Tier E:** recorded as won't-do. Reopen only by owner decision.

Every sub-project follows the defect workflow:

- one commit per item;
- TDD, with a failing test first;
- 100% line and branch coverage of new and changed lines (`tests/check_changed_line_coverage.py --branches`).

## Tier A: bugs (security track, phase S1)

| Item | Status |
|---|---|
| Undo of a pending Follow aimed at a User is ignored (`is_accepted=True` filter, R:1993-1994) | verified |
| `endpoints` without `sharedInbox` raises KeyError at U:1443, U:1578 and U:1856 | verified (a missing `endpoints` is already safe) |

## Tier B: hardening (security track, phases S0, S2–S4)

| Item | Phase | Status |
|---|---|---|
| Fediseer branch (R:811-815) lets an unsigned Create/ChatMessage through | S0 | verified, **found during re-check** |
| `/activities/<type>/<id>` serves any logged outbound activity, DMs included, to anyone | S0 | verified |
| Signed header set not enforced; dead `skip_date` parameter | S2 | verified |
| `keyId` ignored; key taken from body `actor` | S2 | verified |
| No key refetch on signature failure (only relays have one) | S2 | verified |
| Dedupe key set before signature verification, 90 s TTL | S2 | verified, **found during re-check** |
| `/actor/outbox` advertised, no route (`/site_outbox` is not advertised) | S3 | verified |
| Deleted actors and communities return 404, not a 410 Tombstone; `formerType` hard-coded | S3 | verified |
| Community outbox ignores `private`/`local_only` | S3 | verified |
| `/private_message/<id>` ids are not dereferenceable | S3 | verified |
| Announced inner object trusted on the Group's signature | S4 | verified |

## Tier D, security half (security track, phases S5–S6)

| Item | Phase | Status |
|---|---|---|
| RFC 9421 signatures and double-knocking | S5 | verified absent |
| Authorized fetch and secure mode | S6 | verified absent |

## Tier C: interop track (later spec)

All items here are unverified.

- HTML discovery: a `<link rel="alternate" type="application/activity+json">` tag on user, community and post
  pages; an HTTP `Link` header on user and community pages; a `fediverse:creator` meta tag.
- Read legacy quote properties: `quoteUrl`, `_misskey_quote`, `quoteUri`.
- Handle `Update` of a Person or Service, so profile and key refresh no longer waits for the daily job.
- Add Undo handlers for EmojiReact, Flag, Add and Remove.
- Accept a string-valued `endpoints`, which needs a fetch. The security track only stops the crash.
- Content type: serve `application/ld+json; profile="https://www.w3.org/ns/activitystreams"` on request, and send
  both forms in outgoing Accept headers.
- Advertise Person `followers`/`following`, and make the followers route a paged OrderedCollection.
- `following` on Group; `liked`; `likes` and `shares` on objects.
- Person outbox with real content. It is empty today.
- Array-valued `type`.
- `contentMap`/`nameMap`/`summaryMap` with more than one language.
- WebFinger reverse-discovery check; JSON host-meta.
- Accept Create of Audio, Image, Document and Video as first-class objects. This is a product decision.
- Delete of a remote Group.

## Tier D remainder: larger subsystems (one spec each, in order)

1. **Account migration:**
   - `movedTo` and `alsoKnownAs` read, stored and emitted;
   - inbound account `Move` re-points follows;
   - outbound Move;
   - export and import of the following-list CSV.
2. **Inbox forwarding (§7.1.2):** forward replies to a local user's followers-only post to that user's followers.
3. **Delivery addressing:**
   - expand `to`/`cc`;
   - strip `bto`/`bcc` before sending and honour them on delivery;
   - use per-actor inboxes when no shared inbox exists.
4. **JSON-LD expansion:** recognise prefixed and aliased terms in incoming payloads. This only matters for peers
   that use custom contexts. Measure how often that happens before committing to it.
5. **Actor-to-actor Block:** model a Block from one user to another.

## Tier E: won't-do (with reason)

| Item | Reason |
|---|---|
| Client-to-server API (§6), OAuth endpoints, `uploadMedia`, `proxyUrl` | Clients use `/api/alpha`. No C2S client ecosystem targets threadiverse servers. |
| E2EE / MLS | The report is still an early draft, and nothing interoperates yet. |
| LOLA portability | The report is still a draft. Revisit after account migration (D1) lands. |
| FEP-8b32 object-integrity proofs | Few implementations. RFC 9421 plus origin refetch covers the threat. |
| Groups-report `Member` records and Join/Leave model | The draft is unsettled. Follow-based membership interoperates with Lemmy, Mbin and PieFed. |
| Arrive, Ignore, Invite, Join, Leave, Listen, Offer, Read, TentativeAccept, TentativeReject, Travel, View | No peer sends them to a threadiverse server in a form we could act on. |
| Place, Profile, Relationship objects; geosocial | No product use. Event `location` already covers what we need. |
| `generator`, `preview` | Display-only metadata with no product use. |

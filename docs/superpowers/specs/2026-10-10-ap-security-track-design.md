# ActivityPub security track

Status: design approved in session (2026-10-10). It is the first sub-project of
`2026-10-10-ap-gaps-roadmap.md`. Line references are to HEAD `914cd4c00`:
R = `app/activitypub/routes.py`, U = `app/activitypub/util.py`, S = `app/activitypub/signature.py`.

## Problem

The `docs/PYFEDI_GAPS.md` audit, re-checked against current code, shows the following problems:

- The inbox accepts some unsigned activities.
- A route serves private activities to anyone.
- Signature checks accept whatever headers the sender chose to sign.
- A forged request can suppress a genuine activity.
- A Group can put words in other instances' users' mouths.

pyfedi also lacks RFC 9421 signatures and authorized fetch. Both are now common on peers.

## Goals

- No unsigned or mis-signed activity is processed, except through the existing LD-signature and relay paths.
- No non-public object or activity is served to a requester who is not entitled to it.
- Remote key rotation no longer breaks delivery until the daily refresh.
- Announced content that originates on another host is verified before it is stored.
- RFC 9421 signatures are verified inbound, and are sent to peers that refuse draft-cavage.
- Followers-only content can be fetched by authorized peers. An admin can require signed fetches.

## Non-goals

- Interop and larger-subsystem items. These are tiers C and D in the roadmap.
- Verifying announced votes beyond host consistency. This is a residual risk the owner accepted (see S4).
- Serving private or `local_only` communities over ActivityPub to anyone. Those communities never federate
  (`app/models.py:1314`), so no remote party is ever entitled to them.

## Owner rulings (2026-10-10)

| Topic | Ruling |
|---|---|
| Priority | Security first |
| Fediseer unsigned ChatMessage | Rejected like any unsigned activity; the special case is removed |
| `/activities/<type>/<id>` | Public activities served unsigned; non-public served only to an entitled signed requester; otherwise 404 no-store |
| Announce inner objects | Trusted if same host as the Group; otherwise LD signature or origin refetch (Create/Update/Delete) |
| Announced votes and Undo | Host consistency only. Accepted residual: a malicious Group can forge votes from other hosts' users |
| RFC 9421 | Verify both formats inbound; send cavage; on 401 retry once with 9421 and remember the peer's preference |
| Authorized fetch | Signed access unlocks followers-only content; an admin setting can require signed fetches (default off) |
| Banned vs deleted actors | 410 Tombstone for deleted only; banned stays 404 so a ban stays reversible on peers |
| Required signed headers | Logged first behind a flag; enforcement switched on in a separate commit after the owner reviews the logs |
| Coverage | 100% line and branch coverage of new and changed lines |
| Votes refetched by a third host | 404 is fine (only addressees and their hosts are entitled) |
| Cross-origin Delete (Amendment A) | Accepted only from a known moderator or Group-host admin (or a local admin), after an admin-configurable delay (default 90 s) passes without the home server's own Delete; other cross-origin Deletes need the origin to answer 404/410 |
| Followers-only authoring (Amendment B) | Local users may create followers-only posts, in the local `microblogs` community only |

## Shared helpers

The phases share these units. Each one is introduced in the first phase that needs it.

### `signed_requester_actor()`: introduced in S0

This lives in `R`, next to `signed_requestor_domain` (R:2482). It returns the stored actor (User or Community)
whose key validly signed the current GET, or None.

- The keyId is resolved the same way `signed_requestor_domain` resolves it. The function then tries the signer
  as an exact actor URL (`keyId` without its fragment). If that fails, it uses the actor whose stored
  `ap_profile_id` is a prefix of the keyId on the same host. That second rule handles GoToSocial-style
  `/main-key` paths.
- **It never fetches.** An unknown signer returns None.
- A signature that fails verification returns None.
- `signed_requestor_domain` is rewritten to use it. Its fallback stays as it is: when there is no valid
  signature, it returns the User-Agent domain.

### `audience_entitles(addressees, requester, local_actor=None)`: introduced in S0, reused in S3 and S6

This goes in a new module, `app/activitypub/entitlement.py`. It returns True when `requester` (the result of
`signed_requester_actor()`, possibly None) may see an object or activity addressed to `addressees`. The
`addressees` argument is the union of `to`, `cc`, `audience`, `bto` and `bcc`, with strings and lists both
accepted.

It returns True when any of these holds:

1. An addressee is in `AS_PUBLIC` (U:4750).
2. `requester` is not None and its actor id is an addressee.
3. `requester` is not None and the requester's host is the host of an addressed actor. An addressed actor here
   is any addressee other than a followers collection.
4. `requester` is not None, `local_actor` is given, `local_actor`'s followers collection is an addressee, and
   either of these holds:
   - `requester` is an accepted follower of `local_actor`;
   - the requester's host is the host of at least one accepted follower of `local_actor`.

**Rationale for host rules 3 and 4.** Bulk delivery already goes to each instance's shared inbox, so a host that
holds an addressee or a follower has already received the content. Peers refetch our activities to verify
them, and they sign those refetches with their instance actor, not with the addressee.

Everything else returns False.

## Phase S0: critical

### S0.1: fediseer fall-through (R:811-815)

The `elif` whose body is `...` lets an unsigned Create/ChatMessage claiming the fediseer actor reach
processing.

- Delete the branch. That activity now takes the `else` path: log `Could not verify HTTP signature`, 400.
- Delete the fediseer exemption at R:2926 (`user.ap_domain != 'fediseer.com'`). Without the unsigned path it only
  exempts fediseer from the new-account check. Keep the exemption if a test shows fediseer signs and the
  exemption is still wanted. Record the decision in the commit message.
- Test: an unsigned fediseer Create/ChatMessage gets 400 and nothing is stored.

### S0.2: gate `/activities/<type>/<id>` (R:2612)

- Remove `@cache.cached(timeout=2400)`. It keys on the URL alone, so it would serve an entitled requester's 200
  to anyone.
- Load the row as today. Serve it only when
  `audience_entitles(addressees(activity_json), signed_requester_actor(), local_actor)` is True:
  - `addressees` comes from the activity's own fields.
  - `local_actor` is the local User or Community named by the activity's `actor`, or None.
- An activity with no addressing fields is treated as non-public. Older logged rows with an empty
  `activity_json` (`{}`) are refused.
- Entitled public activity: 200 with `AP_CACHE_CONTENT`.
- Entitled non-public activity: 200 with `Cache-Control: private, no-store` and `Vary: Accept, Signature`.
- Refused or missing: 404 with `AP_CACHE_MISS`. Refused and missing responses are identical.
- `/activity_result/<path:id>` (R:2634) is out of scope. It returns only a result string.

## Phase S1: bugs

### S1.1: withdraw a pending follow (R:1993-1994)

Drop `is_accepted=True` from the `UserFollower` filter, mirroring the Feed branch at R:1980-1989. Test: an Undo
of a pending Follow deletes the row. An Undo of an accepted Follow still deletes it.

### S1.2: `endpoints` without `sharedInbox` (U:1443, U:1578, U:1856)

Add a helper `shared_inbox_of(actor_json) -> str | None` in U. It returns `endpoints['sharedInbox']` only when
`endpoints` is a dict and the value is a non-empty str. Otherwise it returns None. All three sites use it, and
each falls back to `inbox` as it already does when `endpoints` is absent. A string `endpoints` is not
dereferenced; that is tier C.

Tests, each at all three sites:

- a dict with `sharedInbox`;
- a dict without `sharedInbox`;
- `endpoints` as a string;
- `sharedInbox` as a non-string.

## Phase S2: signature hardening

### S2.1: required signed headers

- Remove the `skip_date` parameter of `verify_request` (S:441). It is never read. Update its callers (R:801).
- Add a `required: frozenset[str]` parameter to `verify_request`. It defaults to the empty set, which keeps
  today's behaviour for any other callers. The inbox passes
  `POST_REQUIRED = {'(request-target)', 'host', 'date', 'digest'}`. The signed-GET verifier passes
  `GET_REQUIRED = {'(request-target)', 'host', 'date'}`.
- A missing required header is handled by a new env flag, `SIG_REQUIRED_HEADERS_ENFORCE` (config default
  False):
  - False: log at warning with the keyId host and the missing set, then continue.
  - True: raise `VerificationError('unsigned required header: …')`. The existing failure paths, including the
    LD fallback, then apply.
- **Commit 1** adds the check with the flag off. **Commit 2** changes the default to True. It lands only after the
  owner reviews the production warnings. Commit 2 is a separate step in the plan and needs the owner's go-ahead.

### S2.2: keyId and actor consistency

The inbox verifies against the body `actor`'s key (R:801). A valid signature therefore proves possession of
that actor's private key, and a keyId mismatch is not a forgery path. The check is a same-origin rule:

- After the HTTP signature verifies, if the keyId host differs from the actor host, log at info. Accept only if
  `relay_for_forwarded` names an accepted relay, or the activity carries a valid LD signature. Otherwise refuse
  with 400.
- Requests with a matching host are unchanged.

### S2.3: key refresh on failure

Generalise `app/relays/inbound.py:_verified_with_refetch` into a new function,
`verify_with_key_refresh(request, actor, required)`, in S. The relay code calls it too, so there is one
implementation.

1. Verify with the stored key. On success, return.
2. Refetch only if all of these hold:
   - the keyId host equals the actor's host;
   - the actor is remote;
   - `redis.set(f'sig-refetch:{actor.ap_profile_id}', 1, nx=True, ex=300)` succeeds.
3. Make a signed GET of the actor through the existing refresh code, which stores the new key. If the key
   changed, verify once more. Otherwise raise the original error.

This is a single synchronous GET with no sleep and no retry loop, so it is consistent with ruling D738. A fetch
failure raises the original `VerificationError`.

### S2.4: dedupe ordering and window

- Keep the early `redis.exists(id)` check at R:758.
- Move `redis.set(id, 1, ex=…, nx=True)` to after signature verification succeeds. A forged request that reuses
  a genuine id can then no longer suppress it.
- Raise the TTL from 90 s to 86400 s, set by a named constant `ACTIVITY_DEDUPE_SECONDS`.
- The Announce inner-id keying (R:752) is unchanged.

## Phase S3: fetch surface

### S3.1: `/actor/outbox`

Add a route next to the instance actor document (`app/main/routes.py:1323`). It returns an empty
`OrderedCollection` with `totalItems: 0`, the AP content type and the collection cache policy. It is exempt from
S6 secure mode.

### S3.2: Tombstones for deleted actors

- **Local User with `deleted=True`:** AP requests get `tombstone_response(user.public_url(), 'Person')`. HTML
  requests are unchanged.
- **Local User with `banned=True` and not deleted:** 404, as today.
- **Communities: no change, 404 as today.** Local community deletion sets `banned=True`
  (`app/shared/community.py:653`). The community stays restorable for 7 days, and the row is then hard-deleted.
  Deleted and banned are therefore one reversible state. Under the banned-stays-404 ruling, a 410 would tell
  peers to purge a community that may still be restored. If a real `deleted` column is added later, its rows get
  410 with `formerType: 'Group'`.

### S3.3: real `formerType`

`tombstone_response` callers pass the stored type:

- posts: `post.type`, mapped to `Page`, `Article`, `Question`, `Event`, `Link` or `Video`, using the same mapping
  `post_to_page` emits;
- comments: `Note`.

### S3.4: community outbox gate (R:2297)

Return the same response as the community actor document (R:602-603) when `community.private` or
`community.local_only` is set: 403. That rule is permanent; see Non-goals.

### S3.5: `/private_message/<id>`

Add a new AP route.

- **Entitled requester:** the `signed_requester_actor()` is the message's sender or recipient, or is on the host
  of either.
- **Served:** an entitled requester gets the `ChatMessage` as the same `ChatMessage`/`Note` document pyfedi
  federates when it sends one. Headers: `Cache-Control: private, no-store` and `Vary: Accept, Signature`.
- **Refused:** a message that is missing or `deleted`, and every other requester, get 404 with `AP_CACHE_MISS`.
- **HTML request:** redirect to the conversation page. The page's own login checks apply.

## Phase S4: Announce verification

S4 applies in `process_inbox_request` (R:1051) when the Announce `object` is a dict. `process_announced_objects`
re-enters that path for each object, so lists are covered.

Define `group_host = host_of(announce['actor'])`, `inner = announce['object']`, and
`inner_host = host_of(inner['id'])`. Then:

1. **Same origin.** If `inner_host == group_host`, process as today.
2. **Host consistency.** This applies to every cross-origin inner activity: `host_of(inner['actor'])` must equal
   `inner_host`. Otherwise log `Announced activity host does not match its actor` and drop it.
3. **Create/Update:**
   - If the inner object is a dict, its id host must equal `inner_host`. Otherwise drop.
   - Then accept if `inner` carries an LD signature that verifies against the inner actor's key.
   - Otherwise replace the object with its id and run it through `verify_object_from_source` (U:5462), which
     fetches from the origin. A refusal is logged with that function's reason and the activity is dropped.
4. **Delete** (rewritten by Amendment A):
   - **From a moderator or Group-host admin.** The inner actor is one of:
     - a stored moderator or owner of the announcing community (`Community.moderators()`);
     - an admin of the announcing community's host instance (`Community.is_instance_admin`);
     - a local admin.

     Schedule the Delete to be applied after `cross_origin_delete_delay` seconds (see Amendment A). Do not refetch.
   - **From anyone else.** Fetch the deleted object's id:
     - 404 or 410: process. This covers authors deleting their own posts, because the origin's 410 is the home
       server's own word.
     - 200: drop and log `Announced Delete of an object that still exists`.
     - Any other status, or a transport failure: drop and log the status. There is no retry, per D775.
5. **All other types** (Like, Dislike, Undo, Flag, Lock, Add, Remove…): host consistency only. This is the
   accepted residual.

All refetching runs inside the existing Celery processing, never on the request path.

## Phase S5: RFC 9421 signatures

### Module `app/activitypub/signature_rfc9421.py`

It has no new dependency and builds on `cryptography` (pinned at 50.0.2).

- `content_digest(body: bytes) -> str` gives `sha-256=:<b64>:` (RFC 9530).
- `sign(method, url, headers, body, private_key_pem, key_id) -> dict[str, str]` returns the `Signature-Input`,
  `Signature` and `Content-Digest` headers.
  - Covered components: `"@method" "@target-uri" "@authority"`, plus `"content-digest"` when there is a body.
  - Parameters: `created`, `keyid`, `alg="rsa-v1_5-sha256"`.
  - The label is `sig1`.
- `verify(request, public_key_pem, required: frozenset[str]) -> None` raises `VerificationError` on failure.
  - It parses the RFC 8941 structured fields of `Signature-Input` and `Signature`.
  - It rebuilds the signature base (RFC 9421 §2.5) from the derived components above and from any covered
    header fields.
  - Algorithms: `rsa-v1_5-sha256`, `rsa-pss-sha512` and `ed25519`. When `alg` is absent, it is inferred from the
    key type.
  - It checks `Content-Digest` against the body.
  - It rejects a `created` timestamp more than 3600 s in the past or more than 300 s in the future. This matches
    `precheck`'s window.
  - When several signatures are present, the first label that verifies wins.
- `parse_key_id(request) -> str | None` returns the keyid from `Signature-Input`.

### Inbound dispatch

- `HttpSignature.precheck` accepts either `Digest` or `Content-Digest` and validates whichever is present.
- `verify_request`, `verify_with_key_refresh` and `signed_requester_actor` dispatch on `Signature-Input`: when it
  is present, use RFC 9421; otherwise use cavage.
- Every keyId read in S2.2, S2.3, S0 and the relay module goes through one function,
  `request_key_id(request)`, which handles both formats.
- The 9421 equivalents of S2.1's required sets:
  - POST: `{"@method", "@target-uri", "@authority", "content-digest"}`;
  - GET: `{"@method", "@target-uri", "@authority"}`.

  `@target-uri` may be replaced by `@path` together with `@authority`. The same enforcement flag applies.

### Outbound double-knock

- Add a migration with a new column, `Instance.signature_format`: `String(16)`, not null, server default
  `'cavage'`.
- `send_post_request` (S:86) and `signed_get_request` (S:202) sign according to the target instance's
  `signature_format`. An unknown instance defaults to cavage.
- If the response is 401, re-sign once in the other format and resend immediately. If the second attempt
  succeeds (2xx), store the other format on the instance. If it fails, keep the stored value and return the
  second response to the existing failure handling. A 4xx is never queued for retry, so nothing changes there.
- Exactly one alternate attempt is made per send, so a peer that refuses both formats cannot cause a loop.

## Phase S6: authorized fetch

### Unlocking followers-only content

The post and comment AP routes (`post_ap_refusal` R:2499, `comment_ap` R:2450, and the post replies/context
routes that share `post_ap_refusal`) currently return 404 for any visibility outside `OPEN_VISIBILITIES`. A
followers-only object is now served when all of these hold:

- `obj.visibility == VISIBILITY_FOLLOWERS`;
- the author is local;
- `audience_entitles(obj_addressees, signed_requester_actor(), local_actor=obj.author)` returns True. The
  object's addressees are built the way `post_to_page` and `comment_model_to_json` build them.

Served non-public responses carry `Cache-Control: private, no-store` and `Vary: Accept, Signature`. Any
`@cache.cached` on these routes is removed, or keyed on the signer as well as the URL. A refused request gets
the same 404 as today.

Direct-visibility and other non-follower visibilities stay refused. `local_only` and private-community content
stays refused (S3.4).

### Secure mode

- Add a migration with a new column, `Site.require_signed_fetch`: Boolean, not null, server default false. Add an
  admin checkbox in the federation settings form, with help text explaining that unsigned fetchers, including
  some crawlers and tools, will be refused.
- When it is on, an AP GET (`is_activitypub_request()`) with no valid signature gets 401 with an empty body and
  `Cache-Control: no-store`. A valid signature means `signed_requester_actor()` is not None.
- Exempt routes:
  - `/actor`, `/actor/outbox`, `/actor/inbox`;
  - `/.well-known/*`;
  - `/nodeinfo/*`.
- HTML requests are never affected.
- It is implemented as a `before_request` hook on the activitypub blueprint, plus the instance-actor routes'
  blueprint. The exempt list is one module-level constant.
- When `signed_requester_actor()` returns None because the signer is unknown, secure mode refuses the request.
  The instance does not fetch unknown signers on the request path. A peer whose instance actor we have never
  seen gets 401 until we learn of it some other way. This trade-off is accepted, and it matches the no-fetch
  rule of `signed_requestor_domain`.

## Error handling summary

| Situation | Result |
|---|---|
| Unsigned activity, no LD signature, no relay | 400, logged, nothing stored, dedupe key not set |
| Required header unsigned, enforcement off | warning logged, processed |
| Required header unsigned, enforcement on | 400 after the LD and relay fallbacks fail |
| keyId host differs from actor host, no relay or LD signature | 400, logged |
| Signature fails, refetch allowed, key rotated | processed |
| Signature fails, refetch rate-limited or fetch fails | 400 |
| `/activities` or `/private_message` request not entitled | 404 no-store, same as missing |
| Followers-only fetch not entitled | 404, same as today |
| Secure mode, unsigned or unknown signer | 401 no-store |
| Cross-origin Announce, host mismatch | dropped, logged |
| Cross-origin Create/Update, refetch refused or failed | dropped, logged with the reason |
| Announced Delete of an object still served | dropped, logged |
| Outbound 401, alternate format succeeds | delivered; instance format updated |
| Outbound 401, alternate format fails | existing failure handling; format unchanged |

## Testing

These gates are required for every commit:

- TDD: the failing test is committed in the same commit as the fix, and it was observed failing first.
- **100% line and branch coverage of new and changed lines**, using `tests/check_changed_line_coverage.py --branches`.
- No drop in any floor in `coverage_floors.ini`. New floors of 100 for `app/activitypub/signature_rfc9421.py`
  and `app/activitypub/entitlement.py`.
- The inline-import ratchet passes.
- The full suite is green at the end of each phase.

Required cases, in addition to those listed in each phase above:

- **S0:**
  - an unsigned fediseer ChatMessage gets 400;
  - `/activities`, each giving its stated result:
    - public served unsigned (200);
    - a DM requested anonymously (404);
    - a DM requested by its addressee (200, `private, no-store`);
    - a DM requested by a signed non-addressee on an unrelated host (404);
    - a DM requested by a signer on the addressee's host (200);
    - a followers-addressed activity requested by a signer on a follower's host (200);
    - an anonymous request right after an entitled 200 (404);
    - `{}` stored (404);
    - a missing row (404).
- **S1:** see each phase.
- **S2:**
  - each required-header set, missing one header at a time, with the flag off (warning logged) and with it on
    (refused);
  - keyId host mismatch with and without a relay or LD signature;
  - key rotation accepted after one refetch;
  - a second failure within 300 s makes no fetch;
  - a keyId on a foreign host makes no fetch;
  - a forged request reusing a genuine id does not suppress the genuine one;
  - the dedupe TTL is the constant.
- **S3:**
  - `/actor/outbox` shape;
  - a deleted user gets 410 Person and a banned user 404;
  - a deleted or banned community still gets 404;
  - `formerType` per post type;
  - private and local-only community outboxes get 403;
  - private message: sender 200, recipient 200, a recipient's-host signer 200, others 404, deleted 404, HTML
    redirect.
- **S4:**
  - same-origin is unchanged;
  - inner actor host mismatch dropped;
  - cross-origin Create with a valid LD signature accepted without a fetch;
  - cross-origin Create refetched and accepted;
  - cross-origin Create where the refetch is refused;
  - cross-origin Update;
  - Delete with a 410 origin accepted, 200 origin dropped, transport failure dropped;
  - a cross-origin Like with consistent hosts accepted, inconsistent hosts dropped;
  - a list Announce where every element gets the same checks.
- **S5:**
  - RFC 9421 Appendix B.2 test vectors (rsa-v1_5-sha256, and ed25519 B.2.6) verify;
  - a round trip of our own `sign` then `verify`;
  - tampering with `@authority`, `@method`, the body or `Content-Digest` is rejected;
  - an expired `created` is rejected;
  - a missing required component is handled per the flag;
  - with multiple signatures, the first valid one wins;
  - outbound: a fake peer returning 401 for cavage and 2xx for 9421 changes `signature_format`;
  - a peer refusing both formats leaves the format unchanged, makes exactly 2 attempts, and creates no SendQueue
    row;
  - a signed GET double-knock.
- **S6:**
  - a followers-only post fetched by an accepted follower (200, `private, no-store`, `Vary`);
  - by a signer on a follower's host (200);
  - by a non-follower (404);
  - anonymously (404);
  - no cache bleed between signers;
  - secure mode on: unsigned post gets 401, unknown signer 401, known signer 200, each exempt route 200 unsigned,
    HTML unaffected;
  - secure mode off: unchanged behaviour.

## Rollout

- Migrations: `Instance.signature_format` (S5) and `Site.require_signed_fetch` (S6). Both have server defaults,
  so they are safe to apply before the code ships.
- `SIG_REQUIRED_HEADERS_ENFORCE` stays off until the owner reviews the production warnings from S2.1 commit 1.
- Secure mode ships off.
- Each phase is merged and deployed in order, so production logs from earlier phases can inform the later ones.

## Amendment A (2026-10-10): delayed moderator Deletes

**Problem.** A moderator on a third instance removes a post, and the Group announces the Delete. The post's origin
may still serve it, because the removal has not reached it yet or because a moderator removal never deletes it at the
origin. Under the original S4 rule the Delete was dropped, so federated moderation broke.

**Rule.** See S4 item 4.

- **Who counts:**
  - the announcing community's stored moderators and owners;
  - admins of the announcing community's host instance (`InstanceRole` rows for that instance, via
    `Community.is_instance_admin`);
  - local admins.

  `can_moderate` is unchanged, so remote `InstanceRole` admins stay excluded everywhere else.
- **Delay.** A new Celery task, `apply_delayed_delete(announce_json, store_ap_json)`, is scheduled with
  `countdown = cross_origin_delete_delay()`. When it runs:
  - if the object is already gone here, because the home server's own Delete arrived first, log
    `Delete superseded by the home server's` and stop;
  - otherwise process the inner Delete through the normal handler, so it is recorded as a moderator removal.
- **Setting.** `cross_origin_delete_delay` is a `Settings` key read through `get_setting`, with default 90 and
  int coercion falling back to 90. It is not a Site column, so no migration is needed. It appears as an
  IntegerField in the admin federation form, with `NumberRange(min=0, max=3600)`. 0 means apply on the next worker
  run. The pattern follows `relay_retention_days` (`app/relays/expiry.py:18`, `app/relays/forms.py:20`).

**Tests:**
- a moderator Delete is scheduled with the configured countdown, not fetched;
- a Group-host admin Delete is scheduled;
- a Delete from an admin of an unrelated instance takes the refetch path;
- a non-moderator Delete with an origin 410 is processed, a 200 dropped, a 403 dropped, a transport failure dropped;
- `apply_delayed_delete` on an object already deleted logs superseded and changes nothing;
- `apply_delayed_delete` on a live object removes it as a moderator;
- the setting's default, coercion fallback, and form save and populate.

## Amendment B (2026-10-10): Phase S7, followers-only posts in local `microblogs`

**Goal.** A local user can publish a followers-only post in the local `microblogs` community. Only the author's
accepted followers receive it and can see it. This also makes S6's unlock (Task 23) live.

The read side already exists, so this phase is about writing and federating:

- `app/visibility.py` (`can_view`, `listable_clause`, `visible_to_clause`) keeps non-public posts out of every
  listing and shows them to followers on viewer-aware surfaces;
- `refuse_invisible` returns 404 for single-object views.

The microblogs community is identified as today: `Community.name == 'microblogs'` and local, as created by
`find_microblogging_community()` (U:5657). A helper, `is_local_microblogs(community) -> bool`, replaces the
open-coded checks this phase touches.

### S7.1 Creating

- **Web form** (`add_post`, `app/community/routes.py:1184`): a `visibility` SelectField (`public` /
  `followers`, default `public`). It is rendered only when `is_local_microblogs(community)`.
- **Alpha API create** (`app/api/alpha/utils/post.py:1686`): an optional `visibility` (`public` | `followers`).
  Any other value, or `followers` outside local microblogs, gives 400 with `{'error': 'invalid visibility'}`,
  following the API's existing error pattern (ruling D896: API refusals stay 400).
- **Storage:** `make_post` (`app/shared/post.py:204`) stores it on the `Post`.
- **Immutable:** an edit (web or API) that tries to change visibility gives 400, or a form error on the web.
  Visibility is not shown as editable.

### S7.2 Federating a followers-only post

In `send_post` (`app/shared/tasks/pages.py:89`), when `post.visibility == VISIBILITY_FOLLOWERS`:

- **No community Announce.** The `group_announce` and `microblog_announce` paths are skipped.
- **The object is a `Note`,** as the existing second pass builds it, with:
  - `to: [author.followers_url()]`;
  - `cc: [mentioned users]`;
  - `interactionPolicy.canQuote.automaticApproval: [author.public_url()]`.

  The Create/Update wrapper carries the same `to`/`cc`.
- **Recipients:** the shared inboxes of instances that host at least one accepted follower of the author
  (`UserFollower(local_user_id=author, is_inward=True, is_accepted=True)`), plus the instances of mentioned users.
  Each host gets at most one delivery.
- **Edits** use the same addressing and recipients.
- **Delete** already skips community followers for non-open objects (`app/shared/tasks/deletes.py:176`). Its
  addressing is checked and tested so it carries no Public.

### S7.3 Replies under a followers-only post

- A local reply whose parent post (or parent reply) is followers-only is stored with
  `visibility = VISIBILITY_FOLLOWERS`. `make_reply` (`app/shared/reply.py`) sets it from the parent.
- `send_reply` (`app/shared/tasks/notes.py:81`), for a non-open reply:
  - `to: [replier.followers_url()]`, `cc: [parent author's actor id]` plus mentions;
  - no Public anywhere;
  - no community Announce;
  - recipients: the instances hosting the replier's accepted followers, plus the parent author's instance.

  This adds the missing `is_open` gate.

### S7.4 Guards

- `move_object` (`app/shared/tasks/pages.py:402`) and the UI that offers it refuse a non-open post.
- A local boost (Announce) of a non-open post or reply is refused on web and API.
- A local quote of a non-open post is refused.
- The existing E9 gates (likes, locks, adds, removes) apply unchanged. Tests confirm that a followers-only microblog
  post triggers none of them toward community followers.

### S7 tests

- **Web:** the form shows the select only in local microblogs; a followers-only post is stored; posting followers
  to another community is refused.
- **API:** `followers` in microblogs gives 200 and is stored; `followers` elsewhere gives 400; a bad value gives
  400; an edit changing visibility gives 400.
- **Federation:**
  - the Create addressing has no Public anywhere and `to == [followers_url]`;
  - no Announce is sent;
  - the deliveries go exactly to the follower hosts plus the mention hosts, each once;
  - an edit is addressed the same way;
  - the Delete addressing has no Public.
- **Replies:** inherited visibility; addressing; no Announce; a public parent is unchanged.
- **Guards:** move refused; boost refused (web and API); quote refused.
- **Read side** (regression): the post is absent from the community page, RSS, search, the API list and the
  instance timeline; present on a follower's home feed; 404 for a non-follower.

Task 23 (S6 unlock) runs after S7, and its tests use posts authored through S7.1.

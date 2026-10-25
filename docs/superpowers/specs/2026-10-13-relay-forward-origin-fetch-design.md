# Relay forwards without an LD signature: fetch from the origin — design

Date: 2026-10-13. Follows `docs/superpowers/specs/2026-10-08-activitypub-relays-design.md` (relays) and the AP
security track (S2.2, R-LDDOWNGRADE, D738/D775).

## Problem

A Mastodon-style relay (yukimochi Activity-Relay: relay.toot.io, relay.intahnet.co.uk) forwards an author's
activity unchanged and HTTP-signs the request with the relay's own key. `shared_inbox` verifies the HTTP signature
against the activity actor's key (`verify_with_key_refresh(request, actor, POST_REQUIRED)`,
`app/activitypub/routes.py`), which fails for every forward. It then falls back to the body's LD signature:

- an LD-signed body (Mastodon origin) verifies and is processed — these are the relay's 200s;
- a body with no LD signature (Misskey, Pleroma, Akkoma, GoToSocial, … origins), or one whose LD signature does
  not verify, is answered **400** (`Could not verify HTTP signature: Signature mismatch` /
  `Could not verify LD signature: …`). `relay_for_forwarded(request)` is consulted only after the fallback succeeds.

Measured on the first domain, 2026-10-11..13:

- relay.toot.io (933 subscribers): 20,666 POSTs answered 200, 5,366 answered 400 in 48 h — about 20 % of its
  deliveries, and their content, are lost.
- relay.intahnet.co.uk (1,635 subscribers): 136 POSTs in 48 h, in five bursts of 4–8 minutes, each starting just
  after one of our Follows (deploy, relay refresh, admin Retry) and each ending in a run of 3–5 consecutive 400s,
  then silence. 44 of its 45 400s coincide (±2 s) with `Signature mismatch`. 36 relayed posts in 24 h against an
  expected ~4,800 (toot.io's rate scaled by subscribers). The relay appears to stop delivering to us after a run of
  failures; unconfirmed (its nodeinfo reports no version), but the 400s are the part we control.

## Goal

A forward from an **accepted relay** whose own HTTP signature verifies, but which carries no verifiable LD
signature, is answered 2xx and its content is obtained from the author's server, never from the relay's copy.

Non-goals: trusting a relay's signature as proof of the author's content (a relay could then forge any author);
changing LD-signed forwards (unchanged); changing Announce-style relays (`relay_actor_gate`, unchanged); applying
unverified forwarded Updates or Deletes.

## Design

### Where

In `shared_inbox`, in the `except VerificationError` branch after the actor's-key HTTP verification fails, at the
two places that today `return '', 400` (no `signature` in the body; LD signature present but not verifying):
before answering 400, ask `relay_for_forwarded(request)`. If it returns an accepted relay (its keyId owner is
`Relay.actor_id`, state accepted, and the request verifies against `Relay.public_key` with `POST_REQUIRED` — the
existing function), hand the request to a new `relay_inbound.accept_unsigned_forward(relay, request_json,
saved_json, activity_type)` and return its response. If it returns None, the 400 and its log line are unchanged.

### `accept_unsigned_forward(relay, request_json, saved_json, activity_type)` (new, `app/relays/inbound.py`)

Returns a response tuple; always 2xx.

1. **Type.** Only `Create` is acted on. Any other type (Update, Delete, Like, Announce, …) → log ignored
   `relayed {type} without a verifiable signature`, return `('', 200)`.
2. **Object URI.** `object` is a dict with a string `id`, or a string. Anything else → log ignored
   `relayed object has no id`, return `('', 200)`.
3. **Cheap pre-filter** on the forwarded (unverified) body: `relayed_activity_allowed(request_json)`. Refused →
   log ignored with its reason, return `('', 200)`. The unverified body can only make us skip a fetch, never accept
   content, so trusting it here is safe; the same check runs again on the fetched object.
4. **Origin gate.** The object URI's host must equal the activity actor's host (`host_of`), and that host must not
   be banned (`instance_banned(host)`, `app/utils.py`); else log ignored `relayed object host does not match
   actor` / `relayed object from a banned instance`, return `('', 200)`.
5. **Dedupe.** `claim_activity(request_json['id'], relay.actor_id)`: the claim is taken last and under the relay's own
   namespace (`relay.actor_id`), so a relay copy can never claim an author's activity id and a refused forward holds
   no claim. Already claimed → log `APLOG_DUPLICATE`/ignored `Already aware of this activity`, return `('', 200)`.
6. **Queue.** `process_relayed_announce.apply_async(args=(relay.id, uri), queue='background')` (direct call when
   `current_app.debug`, as `relay_actor_gate` does). On an exception while queueing, `release_activity(...)` and
   re-raise (R-DEDUPEUNDO), so the relay's retry is processed.
7. Log `APLOG_CREATE`/success `relayed object queued for fetch from its origin`; return `('', 200)`.

`process_relayed_announce(relay_id, object_uri)` (existing) already does the rest off the request path: skip if
stored; fetch from the origin with `remote_object_to_json(uri, retry_delay=0)` (D775: no retry); re-check
`relayed_object_allowed` on the fetched object; choose the community; create through `create_resolved_object`
(which refuses an object whose `attributedTo` host differs from its id host) under `relay_context(relay_id)` so
`post.relay_id` is set. It keeps its name (its tests and the dedupe tests patch it by name); its docstring says it
serves both relay styles.

### What does not change

- A forward with a verifying LD signature takes today's path (R-LDDOWNGRADE semantics intact).
- A request whose HTTP signature verifies with the actor's key is untouched.
- A request signed by a keyId that is not an accepted relay still gets 400.
- `SIG_REQUIRED_HEADERS_ENFORCE`: the relay's signature is verified with `POST_REQUIRED`, so enforcement applies.
- No fetch, sleep or retry on the request path (D738/D775): the only fetch is in the Celery task.

## Errors and limits

- Volume: ~5k forwards/day across both yukimochi relays today; one background fetch each, deduplicated by activity
  id (step 1) and by stored object (`_exists` in the task).
- A fetch that fails is logged `Could not fetch relayed object` (existing) and dropped; no retry.
- A relay that sends junk gets 200 and nothing is created; the log says why.

## Testing

TDD, 100 % line and branch coverage of changed lines, one commit per defect, gates via `./run_tests.sh`.

- Inbox, forward from an accepted relay, no LD signature, `Create` of a Note: 200; the task is queued with the relay
  id and the object id; nothing is created from the forwarded body.
- Same with an LD signature that does not verify: 200 and queued (no longer 400).
- LD-signed forward: unchanged path (existing tests keep passing).
- KeyId of a relay that is pending, refused, or unknown; or a relay signature that does not verify: 400 as today.
- Non-Create types from a relay without LD signature: 200, nothing queued, logged ignored.
- Object without an id; object host ≠ actor host; banned host; pre-filter refusal (reply to a post we lack):
  200, nothing queued, each logged with its reason.
- Duplicate activity id: second delivery 200 and not queued; queueing failure releases the claim.
- Task: fetched object stored with `relay_id`; fetched object whose `attributedTo` host differs is refused
  (existing behaviour, pinned once for this path).
- `SIG_REQUIRED_HEADERS_ENFORCE` on: a relay signature missing `digest` is not accepted as a relay forward (400).

## Acceptance on production

After deploy, over 24 h: inbox 400s from the yukimochi relays' IPs fall to near zero; relay.intahnet.co.uk
delivers outside the post-Follow bursts; `post.relay_id` counts for relays 7 and 9 rise. If intahnet still goes
silent after a burst, the cause is on its side and its admin is the next step.

## Review focus

- No path creates content from the relay's copy of the object.
- A relay cannot use this to make us fetch from a host other than the actor's.
- Nothing here weakens the 400 for a non-relay keyId mismatch.

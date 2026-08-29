# Sub-project 4: the inbox gate

`app/activitypub/routes.py`'s `shared_inbox`, the three route aliases that
delegate to it, and `replay_inbox_request`. Roughly 200 lines, 7 of them
currently executed under test.

## Why this, and why now

Every previous sub-project in this campaign has tested code that peers reach
*indirectly*. The register reflects that: D21's severity rests on "peer-triggerable
at will", D24's on the `Move` handler being a real caller, D35's on three call
paths. Each of those claims is marked **reading**, not probe. They are almost
certainly right, and none of them has been executed.

`shared_inbox` is where all of them begin. It is the HTTP endpoint a remote
instance actually POSTs to, and everything the campaign has tested so far sits
behind it. Testing it converts a class of reading-only claims into claims with a
test behind them, and it does so at the one place in the codebase where the
threat model is not hypothetical.

It is also, unusually for this file, a **finishable unit**. 139 lines in
`shared_inbox` plus a 55-line replay path, against the 1051-line
`process_inbox_request` behind it. The campaign's habit of slicing large
functions across tasks has cost it real accuracy — three enumerations wrong on
re-derivation in sub-project 3 alone. This one can be covered whole.

## Scope

`shared_inbox` and the routes that delegate to it (`site_inbox`, `user_inbox`,
`community_inbox`), plus `replay_inbox_request`, to 100% statement and branch
coverage or a documented reason, with mutation evidence per guard.

The gate's outcomes, derived from source rather than counted by eye — the task
that opens the sub-project re-derives this table and is expected to find it
wrong somewhere, because every enumeration in this campaign has been:

| # | condition | outcome |
|---|---|---|
| 1 | body is not parseable JSON (`werkzeug BadRequest`) | 400 |
| 2 | client disconnects mid-body (`BlockingIOError`) | 400 |
| 3 | body parses to `None` | 400 |
| 4 | `pause_federation` is `'1'` | 429 |
| 5 | `pause_federation` is `'666'` | 410 |
| 6 | any of `id`/`type`/`actor`/`object` missing | 200 |
| 7 | Announce whose object is a dict with missing fields, typed `Page`/`Note` | 200, logged "Intended for Mastodon" |
| 8 | Announce whose object is a dict with missing fields, any other type | 200, logged as failure |
| 9 | Announce of local content (object actor on this server) | 200, logged duplicate |
| 10 | `allowlist_mode >= ALLOWLIST_STRONG` and actor's host not allowed | 403 |
| 11 | activity id already in redis | 200, logged duplicate |
| 12 | actor URI ends `accounts/peertube` | `''`, logged as a PeerTube view |
| 13 | `HttpSignature.precheck` raises `VerificationFormatError` | 400 |
| 14 | `Delete` of an actor that does not exist here | 200 |
| 15 | actor cannot be found or created | 200 |
| 16 | HTTP signature invalid, no LD signature present | 400 |
| 17 | HTTP signature invalid, LD signature present and also invalid | 400 |
| 18 | HTTP signature invalid, LD signature present and valid | proceeds, `bounced` true |
| 19 | HTTP signature invalid, fediseer ChatMessage exemption | proceeds |
| 20 | success | instance bookkeeping, then dispatch |

Plus the two dispatch paths — `process_delete_request` for an account deletion,
`process_inbox_request` otherwise — each split again on `current_app.debug`.

### Explicitly in scope: the instance bookkeeping

On the success path the gate writes to the actor's instance: `last_seen`,
`dormant`, `gone_forever`, `failures`, and `ip_address` — the last of which is
**blanked when the request arrived bounced** (LD-signature path). That is a
privacy-relevant conditional write on a peer-controlled path and it has no test.
It is in scope and gets its own assertions.

### Out of scope

- **`process_inbox_request`'s body.** The dispatch call is asserted; what it does
  is a successor sub-project. Sub-project 3 established the pattern: assert the
  delegation, not the callee.
- **Fixing anything.** Same rule as 2a and 3. Defects found are registered.
- **The other 50 routes in this file.** `user_profile`, `community_profile`,
  `feed_profile`, the outbox routes and the webfinger handler are all uncovered
  and all out of scope here.

## What makes this harder than anything the campaign has tested so far

**A real signature has to be produced, or the tests are theatre.** Half of this
gate is signature verification. A test that monkeypatched `HttpSignature.verify_request`
would pin nothing at all — it would be the `is_winnable` mistake this campaign
has already made once, in a different file.

It does not have to be faked. `HttpSignature.signed_request(..., send_via_async=True)`
returns `(uri, headers, body_bytes)` **without sending**, so a test can build
genuinely signed headers using production's own signing code and hand them to
Flask's test client. Real signing, real verification, no mock in between. The
same lever inverted — sign with the wrong key, or tamper with the body after
signing — produces the failure cases.

**The request has to arrive through the routing layer.** `shared_inbox` reads
`request`, sets `g.site` itself (with a comment saying `before_request` does not
run for `/inbox`), and returns bare status codes. Tests post through the Flask
test client rather than calling the function, because the thing under test is
the endpoint's behaviour, not the function's return value. `tests/test_request_hooks.py`
already posts to `/inbox`, so the route is known reachable.

**Redis is real here, and the campaign's own notes about it are stale.**
Duplicate suppression and the pause switch both read `redis_client`. The findings
document states that `redis_double` covers `get_redis_connection` and *not*
`redis_client`, and advises a sub-project needing the latter to widen the fixture.
That advice is out of date: `coverage-utils-feed` already widened it, and
`tests/conftest.py` documents the widening. **Correcting that section is a
deliverable of this sub-project** — left standing, it sends the next reader to
redo finished work, which is exactly the failure mode the campaign keeps
recording about its own documents.

**The dispatch split is behaviourally inert.** `current_app.debug` chooses
between an inline call and `.delay()`, and sub-project 3 measured that under
eager Celery `.delay()` runs inline and propagates identically. A recorder is
the honest way to pin which call was made, and the caveat travels with it.

## The quality bar

Unchanged from sub-project 3, and it is the part that matters most here:

- **Every enumeration is derived, not carried.** The table above is this
  document's claim and the first task's job is to check it against source.
- **Assert on what the path did, not on the status code alone.** A 200 is
  returned by six different outcomes in this gate. A test asserting only the
  status is nearly vacuous — it must also assert the log line, the redis key, the
  absence of a dispatch, or the row that was or was not written. Sub-project 3
  had four tests that pinned nothing and every one of them asserted an identity
  or a bare `None`.
- **Mutation, both directions, per guard**, with survivors reported rather than
  chased.
- **`assert_all_called` stays on.** In sub-project 3 it caught four tests that
  never reached the code they named. This gate has more short-circuits than
  anything tested so far, so the same hazard is larger here.

## Verification

1. Every outcome in the table has a test whose assertion distinguishes it from
   the other outcomes sharing its status code.
2. `shared_inbox` and `replay_inbox_request` at 100% statement and branch
   coverage, or a written reason per gap.
3. Mutation evidence per guard, with counts.
4. A first coverage floor for `app/activitypub/routes.py`, set to the measured
   blended figure rounded down, and proven to bite one point higher.
5. The stale `redis_double` section corrected.
6. A `tests/README.md` section covering the signing lever, since it is the
   reusable part.

## Risks

- **The signed-request lever may not survive the test client.** `signed_request`
  builds a `Host` header and a `(request-target)` from the URI it was given, and
  the verification recomputes them from the incoming request. If the test client's
  view of host and path disagrees with what was signed, valid signatures will
  fail for reasons unrelated to the code under test. **The first task is a spike
  on exactly this**, and if it cannot be made to work the sub-project stops and
  re-scopes rather than reaching for a mock.
- **Redis state leaks between tests.** Duplicate suppression writes keys with a
  90-second expiry. Tests that reuse an activity id will interfere with each
  other through the shared Redis unless each generates its own.
- **The gate commits.** `db.session.commit()` runs on the success path before
  dispatch, so tests asserting "nothing was written" need to be precise about
  what "nothing" means.

# Sub-project 8: the webfinger discovery surface

**Date:** 2026-09-02
**Module:** `app/activitypub/routes.py`, the functions `webfinger` and
`process_webfinger_request`
**Predecessors:** 5a-5e covered the inbox dispatcher, 6 its `process_chat`
delegate, 7 its `process_new_content` delegate. This is the first slice of the
module's *outbound* surface.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D142**

## The unit

Two functions, taken together because one is the other's only caller and its
access guard:

| Function | Lines at writing | Uncovered / total |
|---|---|---|
| `webfinger` | 56-72 | **11 / 13** |
| `process_webfinger_request` | 75-168 | **42 / 43** |

**53 uncovered statements.** `process_webfinger_request` is the single largest
uncovered function in the module; `webfinger` is 85% uncovered and did not
surface in a by-size ranking because it is small.

This is how **every remote instance discovers every local actor**. A Mastodon
user typing `@alice@this.instance` reaches this code first, and nothing else
runs until it answers. It is also a federation access-control point: the route
enforces the allowlist and the instance ban list before answering at all.

### Citations drift

Every sub-project since 5c found its brief's line numbers stale, and this
slice's own fixes will move things. **Locate every target by code content.**
Quoted lines are navigational aids, never identifiers.

## What it does

**`webfinger`** (`@bp.route('/.well-known/webfinger')`):

1. If `requestor_domain()` returns a domain, lazily populate `g.site`, then
   either — under `get_setting('use_allowlist')` **and**
   `g.site.allowlist_mode == ALLOWLIST_INTENSE` — refuse a domain that is not
   `instance_allowed()`, or otherwise refuse one that `instance_banned()`.
   Refusal is `abort(403)`.
2. With a `resource` query argument, delegate to `process_webfinger_request`.
   Without one, `abort(404)`.

**`process_webfinger_request(resource)`**, decorated `@cache.memoize(timeout=60)`:

| Step | Behaviour |
|---|---|
| Parse | `'acct:' in query` → actor is `query.split(':')[1].split('@')[0]`; a leading `~` sets `feed = True` and is stripped. Else `'https:' in query or 'http:' in query` → actor is `query.split('/')[-1]`. Else return the string `'Webfinger regex failed to match'`. |
| Instance actor | `actor == SERVER_NAME` returns a fixed JRD for `/actor`, content-type `application/jrd+json`, `Cache-Control: public, max-age=15`, `Access-Control-Allow-Origin: *`. |
| Resolve, not a feed | `User` by `user_name` **or** `alt_user_name` (case-insensitive), filtered `deleted=False, banned=False, ap_id=None`, type `Person`; else `Community` by `ap_profile_id`, filtered `ap_id=None, local_only=False`, type `Group`; else `Feed` by `name`, filtered `ap_id=None`, type `Feed`. |
| Resolve, feed | `Feed` by `name`, filtered `ap_id=None`, type `Feed`. |
| Not found | return `''` |
| Found | a JRD naming `object.public_url()`, plus a `fep/3b86/Create` share template for a `User` or a `fep/3b86/Follow` subscribe template for a `Community`. |

**Delegates**, all imported into `app.activitypub.routes` and patchable there:
`requestor_domain`, `instance_allowed`, `instance_banned`, `get_setting`.
`ALLOWLIST_INTENSE` arrives via `from app.constants import *` (`app/constants.py:142`,
value `2`).

## Goal

Full statement coverage of both functions, and branch coverage sufficient that
no guard survives having any one of its conjuncts dropped. Expected effect:
`app/activitypub/routes.py` moves from its measured **71.7423%** blended toward
**74%**, and the floor rises from 71 to the measured figure rounded down.

## Out of scope

The rest of the outbound surface, each a candidate for its own later slice:
the actor profiles (`community_profile` 41 uncovered, `feed_profile` 31,
`user_profile` 24), the content objects (`post_ap` 24, `post_ap_context` 21,
`comment_ap` 16), and the collections (`feed_following` 18, `feed_outbox` 16,
`community_outbox` 15, `user_followers` 14, and the two moderators routes at 13
each). `process_delete_request` (21) is a `@celery.task`, not an endpoint, and
belongs with the inbox work. The delegates `requestor_domain`,
`instance_allowed` and `instance_banned` are doubled here, not tested here.

## The defects this slice must confront

This slice carries the same bounded fix authorisation 5c through 7 had:
**defects found inside these two functions are fixed test-first, each in its own
commit, separate from every test-only commit, and each proved by a mutation that
fails a named test.** Anything larger is registered.

### The three resolution guards disagree, and the weakest one leaks private feeds

The `User` lookup excludes deleted and banned accounts. The `Community` lookup
excludes `local_only` communities. The `Feed` lookup excludes **nothing** beyond
`ap_id=None`:

```python
object = Feed.query.filter_by(name=actor.strip(), ap_id=None).first()
```

`Feed.public` is `db.Column(db.Boolean, default=False, index=True)` — feeds are
private by default — and `Feed` also carries `ap_deleted_at`. Neither is
checked. So webfinger will confirm the existence of, and publish the URL of, a
**private** feed to any instance that asks, and will keep advertising a deleted
one.

Both other lookups guard exactly this class of thing. That the feed lookup does
not is an asymmetry, not a considered exemption — and it is the same mirrored-guard
shape that yielded four defects in sub-project 7.

### The queried domain is discarded, so this instance answers for other domains

```python
actor = query.split(':')[1].split('@')[0]
```

`acct:alice@evil.example` yields `alice`, and the domain is never looked at
again. If a local user `alice` exists, the response asserts
`"subject": "acct:alice@<our SERVER_NAME>"` with our user's `public_url()`.

RFC 7033 §4.2 has the server return 404 for a resource it is not authoritative
for. Answering for `alice@evil.example` with our own `alice` is an identity
confusion: a client that trusts the reply learns the wrong thing about who owns
that handle. Whether any real client is misled by it is a question the tests
should answer rather than assume; the defect registered is that the domain is
parsed and then thrown away.

### Two wrong status codes

```python
return 'Webfinger regex failed to match'   # malformed resource
...
return ''                                  # no such actor
```

Flask turns a bare string into **HTTP 200** with `text/html`. So a malformed
query gets 200 and a human-readable sentence, and an unknown actor gets 200 and
an empty body. RFC 7033 wants 400 and 404 respectively. The second is the
worse one: a remote instance cannot distinguish "no such user here" from "this
endpoint is broken", and both are indistinguishable from a successful lookup by
status alone.

### `'acct:' in query` is a substring test where a prefix test is meant

`'acct:' in query` matches anywhere in the string, and it is checked *before*
the `http`/`https` branch. `https://evil.example/acct:bob` therefore takes the
acct branch and is parsed by splitting on `:` — yielding `//evil.example/acct`
from `split(':')[1]`, not a URL path segment. Reading-level; whether it is
reachable through the route with a realistic query is for the tests to
establish.

### Also present, registered rather than fixed

- `object` and `type` shadow builtins throughout the function.
- The instance-actor special case runs **after** the `~` feed marker is
  stripped, so `acct:~<SERVER_NAME>@<SERVER_NAME>` returns the instance actor
  rather than resolving a feed. Whether a `~`-prefixed instance-actor query
  occurs in practice is unknown.
- `@cache.memoize(timeout=60)` means a user banned a moment ago stays
  advertised for up to 60 seconds. **This cannot be demonstrated under test** —
  see below — so it is reading-level by necessity.

## Testing approach

**Entry.** `app.test_client()`, driving `GET /.well-known/webfinger?resource=…`
end to end. This is already the suite's established shape — eight existing test
files use `app.test_client()` — so no new infrastructure is needed. Driving the
route rather than calling `process_webfinger_request` directly is what exercises
the allowlist and ban guards, and what makes the status codes above observable
at all.

**`@cache.memoize` is inert under test.** `.env.test` sets
`CACHE_TYPE=NullCache`, so the 60-second memo never stores anything and no test
needs to clear a cache between cases. This is load-bearing: were the cache live,
two tests querying the same `resource` would contaminate each other. It also
means the memoize's staleness behaviour can only ever be registered, never
pinned.

**Nothing currently drives this endpoint.** Every existing mention of webfinger
in the suite mocks a *remote* instance's webfinger through respx
(`tests/conftest.py`'s `federation_peer`, `tests/test_activitypub_util.py`).
None exercises ours. A double of `find_actor_or_create_cached` or a respx route
is neither needed nor wanted here.

**Doubling.** `requestor_domain`, `instance_allowed`, `instance_banned` and
`get_setting` are all imported into `app.activitypub.routes` and are patched
there, following the campaign's binding-site convention
(`record_moderation(monkeypatch, *names)` from
`tests/test_inbox_dispatch_lock_delete.py`). `g.site` is populated from the real
`Site` row `make_site()` creates.

**Seeding.** `make_user`, `make_community` and
`make_feed(instance, name='peerfeed', public=False, local=False, with_keys=False)`
all exist. `seed_community_owner(domain)` runs **before** `make_community` —
the factory hardcodes `user_id=1`/`instance_id=1` against real foreign keys.
`SERVER_NAME` is `test.piefed.local` under test, so the instance-actor branch is
reachable with `acct:test.piefed.local@test.piefed.local`.

**No vacuous assertions.** `make_feed`'s `public` parameter defaults to `False`,
matching the model's own default — a prior sub-project corrected the factory to
make that so. The private-feed test must therefore pass `public=False`
**explicitly**, so the test states its own premise rather than inheriting it,
and a test of the public case must pass `public=True`.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped *separately*, each killed by a distinct named test. The route's access
check has three separable decisions, not one: the walrus
`if requesting_domain := requestor_domain():` (no domain skips both guards
entirely), and then the two-way `and` of `get_setting('use_allowlist')` with
`allowlist_mode == ALLOWLIST_INTENSE`, whose `else` arm reaches
`instance_banned` instead of `instance_allowed`. That is **two conjunct-drop
kills plus both arms of the branch exercised**, and the `requestor_domain()`
falsy path covered on its own. A kill by `respx.models.AllMockedAssertionError`
is an infrastructure kill, not a behavioural one.

**Status and headers are part of the contract**, not decoration. Every test
asserts the response's status code, and the success paths assert
`Content-Type: application/jrd+json`, `Cache-Control` and
`Access-Control-Allow-Origin`. Two of the four defects above are *only* visible
as a status code.

**Docstrings must be true.** A claim about what another test proves is verified
before it is written. Sub-project 7 lost three fix rounds to false docstrings,
two of them written true and falsified later by its own fixes — so after
inverting any defect pin, check which branch that pin used to cover.

**One pytest session at a time.** `tests/README.md` fact 18. Stopping
`run_tests.sh` on the host does **not** kill pytest inside the container
(fact 30); check for survivors before starting a run.

## New test file

One new file, `tests/test_webfinger.py`. At 53 statements across two functions
with a single control flow this is one coherent unit, and the route guard and
the handler are only meaningfully testable together.

No new factories are expected. The plan confirms this against the models rather
than assuming it.

## Global constraints

- Defects found in these two functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered.
- Findings are numbered from **D142**, and both of the register's "Next free
  number" notes are updated in the same change that takes them.
- The coverage floor rises to the measured blended figure rounded down.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a time,
  and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` and
  `scratch_full_cov.json` in the repository root are not the campaign's.

## Success criteria

1. `webfinger` and `process_webfinger_request` reach full statement coverage.
2. No guard survives any one conjunct being dropped. For the route's access
   check specifically: both conjuncts of the allowlist `and` killed separately,
   both arms of that branch exercised, and the no-requesting-domain path
   covered.
3. Every test asserts the HTTP status code; success paths also assert
   content-type and cache headers.
4. Defects found are fixed with a mutation-proved test, or registered with a
   stated reason for not fixing.
5. `coverage_floors.ini` raised for `app/activitypub/routes.py`.
6. The findings register carries every defect found, from D142.
7. Full suite green.

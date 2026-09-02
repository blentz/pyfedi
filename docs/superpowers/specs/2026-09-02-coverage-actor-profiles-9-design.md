# Sub-project 9: the actor-profile endpoints

**Date:** 2026-09-02
**Module:** `app/activitypub/routes.py`, the functions `user_profile`,
`community_profile` and `feed_profile`
**Predecessors:** 5a-7 covered the inbox dispatcher and its delegates; 8 covered
the webfinger discovery surface, the first slice of the outbound side. This is
the second.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D154**

## The unit

Three endpoints, taken together because they are three mirrored implementations
of one idea — *serve an actor as ActivityPub JSON or as HTML, chosen by the
`Accept` header* — and because the defects are in what they do **differently**.

| Function | Lines at writing | Uncovered / total |
|---|---|---|
| `community_profile` | 514-617 | **41 / 49** |
| `feed_profile` | 2647-2733 | **31 / 39** |
| `user_profile` | 365-466 | **24 / 51** |

**96 uncovered statements.** Where webfinger tells a remote instance *that* an
actor exists, these endpoints tell it *what the actor is*: the public key it
signs with, the inbox to deliver to, the shared inbox, the moderators. Every
federated interaction with this instance starts by fetching one of these three
documents.

### Citations drift

Every sub-project since 5c found its brief's line numbers stale, and this
slice's own fixes will move things. **Locate every target by code content.**
Quoted lines are navigational aids, never identifiers.

## What they do

All three share one shape:

1. `actor = actor.strip()`.
2. Resolve: an `@` in the actor means a remote actor; otherwise local.
3. If found and `is_activitypub_request()`: check visibility, build an
   `actor_data` dict, `jsonify` it, set `content_type`, `Cache-Control` and
   `Link`, return.
4. If found and not an AP request: delegate to an HTML renderer.
5. If not found: 404, or something more elaborate.

**The HTML side is nearly free to test.** `user_profile` and `feed_profile`
delegate with a single call (`show_profile(user)`, `show_feed(feed)`), and only
5 of `community_profile`'s 41 uncovered statements sit past its AP-JSON block —
and those are `flash`/`redirect`, not template rendering. This slice is
therefore overwhelmingly about JSON construction, lookup, and visibility.

## The asymmetries — this slice's whole point

Read side by side, the three disagree on almost every axis. Each row is a
candidate defect, and the table is the spec's core claim:

| Axis | `user_profile` | `community_profile` | `feed_profile` |
|---|---|---|---|
| Route methods | `GET, HEAD` | `GET` | `GET` (two routes) |
| Explicit `HEAD` branch | yes | none | none |
| Remote actor + AP request | **served** | `abort(400)` | `abort(400)` |
| Remote lookup guard | `ap_id=...` only | `ap_id=..., banned=False` | `ap_id=..., banned=False` |
| Local lookup guard | `ap_id=None` only | `ap_profile_id=..., ap_id=None` | `name=..., ap_id=None` |
| Remote-handle fallback | `resolve_remote_handle` | none | none |
| Visibility guard | **none** | `local_only or private` → 403 | `not public` → 403 |
| `Cache-Control` | `max-age=15` | `max-age=30` | `max-age=5` |
| `Vary: Accept` | yes | yes | **no** |
| Not-found path | `abort(404)` | 404 / two redirects / 404 | `abort(404)` |

## Goal

Full statement coverage of all three functions, and branch coverage sufficient
that no guard survives having any one of its conjuncts dropped. Expected effect:
`app/activitypub/routes.py` moves from its measured **74.7804%** blended toward
**79%**, and the floor rises from 74 to the measured figure rounded down.

## Out of scope

The rest of the outbound surface, each a candidate for a later slice: the
content objects (`post_ap` 24 uncovered, `post_ap_context` 21, `comment_ap` 16),
the collections (`feed_following` 18, `feed_outbox` 16, `community_outbox` 15,
`user_followers` 14, the two moderators routes at 13 each),
`announce_activity_to_followers` (19), `lemmy_federated_instances` (17), and
`process_delete_request` (21, a `@celery.task` belonging with the inbox work).

The HTML renderers `show_profile`, `show_community` and `show_feed` are doubled
here, not tested here. `resolve_remote_handle` and `default_context` likewise.

## The defects this slice must confront

This slice carries the same bounded fix authorisation 5c through 8 had:
**defects found inside these three functions are fixed test-first, each in its
own commit, separate from every test-only commit, and each proved by a mutation
that fails a named test.** Anything larger is registered.

### Authorised for fixing

Two defects in `user_profile`, both explicitly authorised.

**The admin branch is dead code.** `user_profile` opens:

```python
# admins can view deleted accounts
if current_user.is_authenticated and current_user.is_admin():
    <six lines>
else:
    <the same six lines, byte for byte>
```

Verified identical across all six lines. The branch is a pure no-op, and its
stated purpose — letting admins see deleted accounts — is unimplemented.

**Neither branch filters `deleted` or `banned`.** So `/u/<actor>` serves the
full actor document of a deleted or banned user to anyone, while webfinger's
user lookup correctly excludes both (`deleted=False, banned=False`). Two
endpoints, the same actor type, opposite answers. The comment's intent runs the
other way from the code's effect: it wanted admins to retain access that
everyone in fact already has.

**Fixing this changes who is visible** — a deleted user's profile will start
returning 404 where it now returns data. That is the intended change, decided
deliberately.

### Registered by default; the plan may propose fixes

The remaining asymmetries are registered unless the plan argues a specific one
is small, self-contained, provable by mutation, and inside these three
functions. The strongest candidate is the first.

**`feed_profile` omits `Vary: Accept`.** The other two set it. An endpoint whose
response body depends on the `Accept` header and which does not say so is a
cache-correctness bug: any shared cache may serve the ActivityPub JSON to a
browser, or the HTML to a remote instance. One line, no behavioural risk to
anything but caching, and mutation-provable by asserting the header.

**`user_profile` serves remote actors' ActivityPub documents.** The other two
`abort(400)` for an AP request about a remote actor, with the comment "don't
provide activitypub info for remote communities". `user_profile` has no such
guard, so this instance will answer with a remote user's actor document
carrying **our** `sharedInbox` and `attributionDomains`. Whether that is
impersonation or a deliberate convenience is a federation-behaviour question
this slice should not settle alone.

**The local lookups have no ban guard.** `community_profile`'s *remote* lookup
filters `banned=False` and its *local* lookup does not; `feed_profile` has the
identical split. This is D153 — registered in sub-project 8 for webfinger's
community lookup — reappearing in two more endpoints, which strengthens the
case that it is a systematic oversight rather than three independent choices.

**`preferredUsername` and `id` reflect the caller's casing.** All three build
`id` and `preferredUsername` from the raw `actor` path segment while resolving
with `actor.lower()`, so `/c/BOOKS` returns `id` `https://server/c/BOOKS` and
`preferredUsername` `BOOKS` for a community whose canonical profile is
lowercase. A remote instance that stores what it is told will hold an id that
differs from the canonical one.

**Three different `Cache-Control` max-ages** (15, 30, 5) for three documents of
the same kind and volatility, with no evident reason.

**`community_profile`'s not-found path branches on authentication** and
redirects to two different UI routes, while the other two `abort(404)`.

## Testing approach

**Entry.** `app.test_client()`, driving `GET /u/<actor>`, `/c/<actor>` and
`/f/<actor>` with and without an ActivityPub `Accept` header. This is the
harness sub-project 8 proved on webfinger; no new infrastructure is needed.

**`user_profile` is already half-covered, and by tests that are not ours.**
`tests/test_remote_handle_resolution.py` and `tests/test_request_hooks.py` both
drive `GET /u/<actor>`, which is why the function measures 52.9% rather than
near zero: its AP-JSON happy path, both lookup branches and every optional
field's *false* side already execute. Its 24 uncovered statements are the admin
branch's local-lookup body, the `ap_profile_id` fallback, the whole `HEAD`
branch, every optional field's *true* side, and the `show_profile` delegation.
**The plan reads both files before writing anything**, so the new tests
complement rather than duplicate them — and so that a later fix to
`user_profile` is checked against the tests that already depend on it. Neither
`community_profile` nor `feed_profile` has an equivalent, which is part of why
they sit at 16% and 20%.

**`is_activitypub_request()` is the switch every test turns.** It reads the
`Accept` header. Tests set a real header rather than doubling it, the way
sub-project 8 drove `requestor_domain()` with a real `User-Agent` — the parse is
part of what is under test. The plan confirms exactly which values it accepts.

**Doubling.** `show_profile`, `show_community`, `show_feed` and
`resolve_remote_handle` are imported into `app.activitypub.routes` and patched
there, following the campaign's binding-site convention.
`resolve_remote_handle` in particular reaches the network and must never run.

**Seeding.** `make_user`, `make_community`, `make_local_feed` (added in
sub-project 8) and `seed_community_owner` all exist. A local actor needs
`ap_id=None`; `make_user(..., local=True)` and `make_community` both do that,
and `make_feed` does **not** — which is why `make_local_feed` exists.

**No vacuous assertions.** Every column these functions branch on —
`User.deleted`, `User.banned`, `Community.banned`, `Community.local_only`,
`Community.private`, `Feed.public`, `Feed.banned` — has a declared default, so
every test that depends on one sets it explicitly.

**Optional-field branches are the bulk of the statements.** Each function has
paired `if <field>_id is not None:` blocks for an icon and a header image, each
with an `http`-prefix branch and a relative-path branch, plus optional summary,
theme, matrix id, extra fields, languages and child feeds. These are where the
96 statements live. They are cheap to cover and worth covering, but the plan
must not let them crowd out the guards, which is where the defects are.

**Every test asserts `response.status_code`**, and AP-JSON tests also assert
`content_type`, `Cache-Control`, `Link` and — where present — `Vary`. Sub-project
8 established that status-only assertions can be blind; here the headers are
themselves part of the contract and one of them is a registered defect.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped separately, each killed by a distinct named test. Note the pattern
sub-project 8 hit four times: **a filter clause whose value equals what the
factory always produces cannot be killed by any test using that factory
unmodified.** All three local lookups filter `ap_id=None`, and every local-actor
factory produces exactly that — so those clauses will be unkillable unless a
test sets `ap_id` explicitly to something contrary.

**Docstrings must be true**, including after the fixes. Sub-project 7 lost two
fix rounds to docstrings written true and falsified by its own later fixes;
after inverting any pin, check which branch that pin used to cover.

**One pytest session at a time**, and stopping `run_tests.sh` on the host does
not kill pytest in the container.

## New test file

One new file, `tests/test_actor_profiles.py`. The three endpoints share a
harness, a seeding surface and — crucially — the comparisons that make their
asymmetries visible; splitting them into three files would hide exactly what
this slice exists to find.

No new factories are expected. **The plan confirms this against the models
rather than assuming it** — sub-project 8's spec made the same claim and was
wrong, because `make_feed` sets `ap_id` unconditionally and no feed it built was
reachable.

## Global constraints

- Defects found in these three functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered.
- Findings are numbered from **D154**, and **both** of the register's "Next free
  number" notes are updated in the same change.
- The coverage floor rises to the measured blended figure rounded down.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a time,
  and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` and
  `scratch_full_cov.json` in the repository root are not the campaign's.

## Success criteria

1. All three functions reach full statement coverage.
2. No guard survives any one conjunct being dropped, each kill by a distinct
   named test.
3. Every test asserts the status code; AP-JSON tests also assert the response
   headers, which are part of the federation contract.
4. `user_profile`'s dead admin branch is removed and its `deleted`/`banned`
   guards added, both test-first and mutation-proved.
5. Every other asymmetry in the table above is either fixed with a
   mutation-proved test or registered with a stated reason.
6. `coverage_floors.ini` raised for `app/activitypub/routes.py`.
7. The findings register carries every defect found, from D154.
8. Full suite green.

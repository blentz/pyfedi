# Fuzzing every external-facing route — design

Date: 2026-11-13. Owner ruling: "all external-facing routes must be fuzzed". Extends the fuzz-all-surfaces track
(spec 2026-10-13), which fuzzes parsers with no database; this track fuzzes the routes themselves, through Flask, as a
request from outside reaches them.

## 1. Scope

Every rule in `app.url_map` is external-facing: every blueprint, `/dev` and `/admin` included, is registered in
production, and `/static` serves files from disk. About 907 route decorators across 25 packages
(api 153, mastodon 100, admin 95, post 80, user 72, community 64, main 62, peertube 61, activitypub 53, lemmy 42,
auth 30, chat 16, feed 14, the rest under 10 each).

A route is covered when a route target can reach it. Coverage is checked against `url_map` itself, not a list kept by
hand: a route added later is covered or the gate fails.

## 2. The route harness

`tests/fuzz/routes.py`: one engine, many targets. A route target names a family of routes (by blueprint, §4) and
turns its input bytes into one request:

- which rule of the family, and which of its methods;
- a value for each path converter (`int`, `string`, `path`, `uuid`, ...): a seeded record's id or name, or fuzzed
  text/numbers (negative, huge, unicode, `..`, NUL, very long);
- a query string, a body (form, JSON, multipart with a file, raw bytes with any content type) and headers (Accept,
  Content-Type, Signature, Authorization, User-Agent, X-PieFed-Via for the onion), each drawn from the input;
- who asks (§3).

The request goes through Flask's test client, inside one SAVEPOINT rolled back after it, so every input meets the same
seeded state. Outbound HTTP is refused (the request helpers raise), Celery runs inline (as the suite does), and mail is
suppressed.

**The oracle.** A finding is:
- an unhandled exception, or any 500;
- a response slower than the target's per-input budget (a ReDoS or quadratic shape);
- for the APIs with an error format of their own (Mastodon, PeerTube problem+json, Lemmy, alpha, OAuth), a 4xx that
  is not in that format.

Deliberate 502/503/504 (an upstream refused or down) are not findings.

## 3. Who asks

Each request is made as one of: nobody; a local member; a community moderator; an admin; a banned account; and,
for the APIs, each credential they take (alpha JWT, OAuth bearer of each scope, PeerTube token, gpodder app password,
a session cookie with and without CSRF). On the onion, the same through the onion's own path (X-PieFed-Via).

## 4. Targets, one per family

`routes_<family>`, each with its own corpus and budget: `routes_auth`, `routes_oauth`, `routes_gpodder`,
`routes_activitypub` (inbox POSTs with fuzzed signatures and bodies, actors, webfinger, nodeinfo), `routes_alpha`,
`routes_mastodon`, `routes_peertube`, `routes_lemmy`, `routes_web` (main, post, community, user, feed, chat, search,
tag, domain, topic, instance), `routes_admin`, `routes_dev`, `routes_payments`, `routes_static`. The family of a rule is
its blueprint, from a committed map; a blueprint the map does not name fails the gate.

## 5. Gates

1. `tests/test_fuzz_routes_registry.py`: every rule in `url_map` belongs to exactly one family with a target; every
   family's target can reach each of its rules (the engine's rule selection covers them all); a route excluded must
   carry a reason, and there are none to begin with.
2. The corpus replay (in the suite, with the database): every committed route seed replays without a finding, within
   its budget. Each family is seeded with at least one request per rule.
3. The smoke campaign runs the route targets too, a few seconds each.

## 6. Tiers

- T1 before login: auth, OAuth, gpodder, ActivityPub (inbox, actors, webfinger, nodeinfo), static.
- T2 the client APIs: alpha, Mastodon (and the Pixelfed shim), PeerTube, Lemmy v3.
- T3 the web pages.
- T4 admin, dev, payments.

Findings are fixed test-first, one commit per defect; a fix that changes behaviour beyond a clean refusal goes to the
owner first.

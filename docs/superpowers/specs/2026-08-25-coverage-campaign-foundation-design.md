# Coverage Campaign: Test Infrastructure Foundation

Date: 2026-08-25
Status: Approved design, ready for implementation planning

This is sub-project 0 of a campaign to bring PieFed to 100% statement and branch
coverage. It builds the mocking layer everything else depends on. The campaign's
overall structure is recorded here too, because sub-project 0's decisions — the
ratchet, the exclusion policy, the quality bar — govern all fifteen.

## The campaign

Measured on 2026-08-25 at commit 4a00b8f5: **37,983 statements, 27,849 uncovered,
14,634 branches — 21% covered.**

| Subsystem | Statements | Uncovered |
|---|---|---|
| `app/api` | 7,360 | 4,360 |
| `app/activitypub` | 5,137 | 4,472 |
| top level (`models.py` 2,911, `utils.py` 2,770, `cli.py`, `email.py`, …) | ~8,845 | ~6,000 |
| `app/shared` | 3,037 | 2,632 |
| `app/community` | 2,947 | 2,382 |
| `app/admin` | 2,242 | 1,667 |
| `app/user` | 2,096 | 1,657 |
| `app/post` | 1,905 | 1,539 |
| `app/nntp` | 1,121 | 1,121 (0%) |
| `app/auth` | 961 | 712 |
| `app/main` | 898 | 697 |
| `app/feed`, `app/instance`, `app/tag`, `app/chat` | 1,434 | 1,132 |

Each subsystem is its own sub-project with its own spec, plan and implementation
cycle. This is a program of work measured in weeks of sessions. Stating that plainly
is part of the design: a campaign that pretends otherwise gets abandoned halfway with
nothing enforcing what it achieved.

Order is by leverage rather than size. Sub-project 0 first, because four subsystems
cannot be tested honestly without it. Then the top level (`models.py`, `utils.py`),
because every other subsystem exercises them incidentally. Then `app/activitypub`,
the largest true gap. Then the route blueprints. `app/nntp` last: self-contained, at
0%, and needing a protocol-level harness of its own.

## Why sub-project 0 comes first

`app/activitypub` and `app/shared` are dominated by code that reaches the network,
S3, Redis or Celery. Today the harness has none of those doubled, so that code is
not merely untested — it is untestable, and the campaign would stall on its second
sub-project.

The alternative is worse than waiting: without shared fixtures, each sub-project
hand-rolls its own mocks, inconsistently, and they get rewritten when the next
sub-project needs the same peer with different responses.

## Scope

### In scope

1. Test dependencies: `moto` (S3), `respx` (outbound HTTP), `fakeredis` (Redis),
   and Celery eager mode via configuration.
2. Shared fixtures for each, in `tests/conftest.py`.
3. A federation-peer fixture: webfinger, actor JSON and outbox responses, plus a
   helper for constructing HTTP-signature-verified inbound activities.
4. `.coveragerc` with per-module floors and the exclusion policy.
5. One module raised off its floor using the new fixtures, proving they work.

### Out of scope

- **Covering any subsystem.** That is sub-projects 1 onward. Sub-project 0 delivers
  capability, plus one proof.
- **Fixing the 216 existing inline imports.** They violate a project rule (below),
  and some exist to break real import cycles, so resolving them is architectural work
  with its own risk. A coverage campaign must not quietly become an import
  refactor. Recorded as its own future project.
- **`app/nntp`.** Its protocol harness is a sub-project, not a fixture.
- **CI wiring.** No CI job currently runs pytest at all (`.woodpecker.yaml` builds
  images; `.forgejo/workflows/test.yaml` runs Ruff). The ratchet is therefore enforced
  by whoever runs the command until that changes. Making CI run the suite is worth
  doing and is not this sub-project.

## Project rules that govern every sub-project

These came from the project owner and bind all code the campaign writes:

1. **`if TYPE_CHECKING` is always a bug.** Never introduce it. It currently appears
   **zero** times in `app/`, and it must not be added to `exclude_lines`, where it is
   a common default.
2. **Imports go at the top of the file. No inline imports.** `app/` currently
   contains 216 violations, including one added by the request-hooks work
   (`app/__init__.py`'s `register_request_hooks` import, which mirrors the existing
   `load_plugins` import above it and exists to break a real cycle). The campaign adds
   no new violations. Existing ones are catalogued for the separate cleanup project.

## Architecture

### Dependencies

`respx` rather than `responses`, because this codebase's outbound HTTP is `httpx`
(`requirements.txt:15`, used throughout `app/activitypub/util.py` and `app/utils.py`);
`responses` patches `requests` and would silently match nothing.

`moto` for S3 — `boto3` appears in `app/email.py`, `app/main/routes.py` and
`app/admin/util.py`.

`fakeredis` for Redis. Note `app/__init__.py:61-62` builds the rate limiter and Celery
app from `Config` at import time, so the Redis double must be reachable via
environment before import, exactly as `.env.test` already handles the database.

Celery eager mode is configuration, not a library: `task_always_eager` plus
`task_eager_propagates` so a failing task raises in the test rather than being
swallowed.

### Fixtures

All in `tests/conftest.py`, alongside the existing `app`, `db_session` and `site`:

- `s3_bucket` — a moto-backed bucket, yielding the bucket name.
- `http_mock` — a `respx` router, asserting all mocked routes were called so a test
  that stubs a request it never makes fails rather than passing silently.
- `redis_double` — a `fakeredis` instance patched over `app.redis_client`.
- `federation_peer` — builds on `http_mock`: registers webfinger, actor and outbox
  responses for a named remote handle, returning the actor dict so tests can assert
  against what the peer actually served.
- `signed_activity` — constructs an inbound activity with a valid HTTP signature, so
  inbox handling can be exercised without a live peer.

### Coverage configuration

A `.coveragerc` replaces the current all-CLI-flags approach, carrying:

- `branch = True`
- `exclude_lines`: `pragma: no cover`, `if __name__ == .__main__.:`,
  `raise NotImplementedError`, `@(abc\.)?abstractmethod`. **Not** `TYPE_CHECKING`.
- Per-module floors, seeded with `app/request_hooks.py` at 100.

Floors only rise. A finished module regressing fails immediately; an unfinished one
blocks nobody.

Note `--cov=app.request_hooks` (dotted) works and `--cov=app/request_hooks.py` (path)
silently measures nothing — `tests/README.md` already documents this, and the
`.coveragerc` must not reintroduce the broken form.

### The quality bar

Coverage is a floor, not the goal. Stated here because it governs fifteen
sub-projects and because a 100% target has already produced three inert tests in this
codebase — tests that passed against deleted production code, caught only by
adversarial review.

- Every test asserts on observable behaviour: a response header, a database row, a
  `g` value, a return value. Never on whether a mock was called, unless the call
  itself is the behaviour under test.
- Every pragma carries a comment saying why the line is unreachable. Reviewers reject
  unexplained pragmas.
- An honest gap with a named reason beats a hollow test that reaches the number.
- Branch coverage is not condition coverage. A compound condition can be fully
  "covered" while one of its sub-conditions is never falsified. Where a sub-condition
  matters, it gets its own test.

## Verification

The dependencies are proved by use, not by importing them:

1. `app/instance/util.py` (25 statements, 34% covered) rises to 100%. Its
   `bulk_follow` calls `search_for_user`, which performs webfinger and actor fetches
   — exactly what `federation_peer` exists to serve. If that module cannot reach 100%
   with the new fixtures, the fixtures are wrong and the sub-project is not done.
2. The `.coveragerc` floors are checked: `app/request_hooks.py` must still gate at
   100, and dropping a test from it must fail the run.
3. The full suite stays green: 317 passed, 0 failed at the time of writing.

## Risks

- **The fixtures encode assumptions about remote behaviour.** A `federation_peer` that
  serves responses no real Mastodon sends produces tests that pass while production
  fails. The actor and webfinger payloads must be taken from real captured responses,
  not invented — `docs/activitypub_examples/` holds examples to work from.
- **Mocking makes hollow tests easier, not harder.** Every mock is an opportunity to
  assert on the mock. The quality bar above exists for this, and it is the thing most
  likely to erode over fifteen sub-projects.
- **Import-time configuration.** The rate limiter and Celery app are built from
  `Config` at import, so a Redis double installed after import affects nothing. The
  fixture must work through the environment, and a test that appears to use the double
  while actually hitting a real Redis would be silently wrong.
- **`app/instance/util.py` may not reach 100% honestly.** Its `except Exception:` path
  re-raises after a rollback; inducing it requires making a mocked call raise, which is
  legitimate. If some line still resists, the sub-project reports the gap rather than
  contorting — and that report is a finding about the fixtures, not a footnote.

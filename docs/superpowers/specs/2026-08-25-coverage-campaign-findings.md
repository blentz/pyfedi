# Coverage Campaign: findings carried forward from sub-project 0

Date: 2026-08-25
Status: living document — sub-projects 1-14 read this before starting, and add to it

Sub-project 0 built the test-infrastructure foundation. These are the things it
learned that the remaining fourteen sub-projects need and cannot re-derive
cheaply. They are recorded here, in git, rather than only in the sub-project's
gitignored working directory.

## The dominant failure mode: correct behaviour, false explanation

Across sub-project 0, **nine of the nine Important review findings** were the
same shape. The code worked. The comment, docstring or README paragraph saying
*why* it worked was wrong — sometimes elaborately, confidently wrong, with
citations to source files that did not say what they were claimed to say.

This is more dangerous than a bug. A bug fails a test. A false explanation gets
believed and built on, and it stops the reader checking.

Three concrete instances, all caught only by a reviewer who executed the claim
rather than reading it:

- A 30-line comment explaining Celery configuration via a `ChainMap` lookup
  and a literal `task_always_eager` default. Neither exists in this codebase's
  configuration. The setting worked for a different reason entirely.
- A documented recipe for asserting federation delivery that could never pass,
  because the factory it told you to use builds users without a private key.
- A docstring asserting respx router precedence in exactly the wrong direction.

**What to do about it.** For any claim in a comment, docstring or doc that a
future reader would rely on: verify it by execution, or write the weaker claim
you can actually support. "Verified against Celery 5.3.6" is worth more than a
mechanism story. Prefer a rule plus the command that regenerates it over an
enumeration — enumerations rot silently (one in this sub-project named 3 modules
where `grep` found 14, *in the commit that was fixing that same defect*).

## Verify a test by deleting the code it names

This codebase has now produced several tests that passed against production code
that was absent, deleted, or never reached:

- Tests satisfied by Flask's automatic OPTIONS response rather than the handler
  under test.
- `test_redis_double_rejects_a_malformed_uuid`, which passed with the entire
  regex guard deleted from `decode_captcha` — a guardless lookup simply misses
  and returns `False` anyway.
- Three inert tests in an earlier sub-project, caught only by adversarial review.

Before believing a new test, name the production change that would make it fail.
If you cannot name one, the test is decoration. Where it is cheap, actually make
that change and watch the test fail.

## Harness properties you must know before writing tests

### Eager Celery runs federation inline, and failures are swallowed

`task_always_eager` is on. `.delay()` executes in-process, so `task_selector`
and `send_post_request` really do attempt delivery during a test.

`app/activitypub/signature.py`'s `post_request` wraps the send in
`except Exception` and records an `ActivityPubLog` row instead of re-raising.
`task_eager_propagates` does not help — it re-raises what the *task* raises, and
this task raises nothing.

**A green federation test therefore proves nothing about delivery.** To assert an
activity was sent, either pass `include_inbox=True` to `federation_peer` **and**
build the sender with `make_user(..., with_keys=True)` (signing dereferences the
private key, so a keyless sender dies before any HTTP happens), or assert on the
`ActivityPubLog` row. `tests/test_fixture_proofs.py::test_delivery_can_be_proved_when_the_sender_has_keys`
is the worked example.

### `block_outbound_http` blocks httpx and nothing else

Session-scoped and autouse. It exists because eager Celery turned every
federating test into real outbound timeouts (`tests/test_announce_dispatch.py`
measured 1.15s with it, 119s without).

It does **not** block:

- `urllib` — `app/nntp/server.py:767`
- `boto3`/`botocore` — ten modules; `s3_bucket` is opt-in
- `smtplib` — `app/email.py`

Those still reach the real internet from the test suite. **`app/nntp` is
entirely socket work and sits at 0% coverage** — that sub-project should expect
to design a socket-level block, and is deliberately last in the campaign order
partly for this reason.

respx consults routers in *registration* order, so the session router is asked
first and `http_mock` second. This is safe only because it registers zero
routes. **Never add a route to it** — a catch-all would silently override every
`http_mock` route in the suite.

### `redis_double` covers `get_redis_connection`, not `redis_client`

`from X import Y` binds a new name in the importing module at import time, so
each binding must be patched separately. The fixture patches all four
`get_redis_connection` bindings.

`app.redis_client` — the global `create_app()` assigns — is read via
`from app import redis_client` in roughly fourteen modules
(`grep -rn 'from app import.*redis_client' app/` for the current set). A
sub-project needing Redis isolation across `app/` should widen `redis_double`
rather than hand-roll its own. The rate limiter and Celery app are built from
`Config` at import time and are outside any fixture's reach.

## Production defects found, not fixed

These are real application problems that the coverage work surfaced. They are
out of scope for a coverage campaign but should not be lost.

- **A genuine circular import.** `app.community.routes` needs
  `RsaKeys`/`send_post_request` from `app.activitypub.signature`, and
  `app.activitypub.routes` needs `show_community` from `app.community.routes`.
  Whichever package `__init__` runs first wins; the loser raises `ImportError`.
  `tests/conftest.py` primes `import app.activitypub.signature` to fix it
  session-wide, which only protects entry points going through that conftest.
  This is related to the project's no-inline-imports rule, since inline imports
  are the usual way such cycles get papered over.
- **216 inline imports in `app/`**, violating a project rule. Catalogued as its
  own future project — some exist to break real cycles, so resolving them is
  architectural work. A coverage campaign must not quietly become an import
  refactor.

### Permission call-site audit — 14 unguarded paths, deferred by ruling

`docs/superpowers/specs/2026-08-25-permission-callsite-audit.md`

Sub-project 1b-i's last task audited the eight permission functions
**backwards**: instead of checking the call sites, it enumerated every entry
point that creates a post, creates a reply, records a vote, or accepts an upload
— by searching for what the action *does* (`Post(`, `PostReply(`, `PostVote(`,
`PostReplyVote(`, the upload entry points) rather than for the guard's name,
because a path that never calls the guard cannot be found by grepping for it.

68 rows across web routes, the API, the ActivityPub inbox, the NNTP gateway,
CLI commands and Celery tasks. **14 rows / 10 entry points / 5 sinks are
unguarded.** The project owner has ruled: **document and defer**, to be
evaluated once the campaign's testing work lands, because better coverage of the
surrounding code changes what a safe fix looks like. They are accepted, not
overlooked.

Ordered by exposure: `GET /api/alpha/resolve_object` (auth is optional, so an
anonymous caller can persist remote content); poll voting on all three of its
entry points (no permission check at all); `create_resolved_object`
(signature-checked and impersonation-checked, but no ban or allowlist
enforcement); `resolve_remote_post_from_search` (the AP `Move` path has no
requester); and `retrieve_mods_and_backfill` (no attributedTo domain-match, so
any instance can be attributed).

Read the audit's **Status** section before acting on any of them — several have
real partial protection, and it records what upstream *does* provide as
carefully as what it does not.

Two things there matter beyond the deferred set:

- **The method generalises.** Any sub-project that covers a guard should also
  ask which entry points skip it. This campaign's largest find, `sanitize_svg`,
  was that shape: the function was correct and five of seven upload paths never
  called it. A forward audit cannot see that; searching by effect can.
- **`can_upload_video` is one defect seen from two directions.** Task 7 found
  its `'users'` branch ignores its injected user; this audit found `make_post`
  and `edit_post` call it with no user at all. Neither half is complete alone —
  the audit cross-references both.

Two more things surfaced during this branch's final review, after the ruling
above, and are recorded in the audit doc rather than here so they stay next to
the evidence:

- **One of the fourteen now has a tracked follow-up, not just a table row.**
  Of the deferred set, `GET /api/alpha/resolve_object` (item 1 / F3) is the
  only one reachable with no credential at all — auth is optional, and
  `enable_api()` is the sole gate. The ruling above (document and defer) is
  unchanged; what changed is that this one item is now called out on its own
  so it does not wait for the whole batch. See "Tracked follow-up: item 1" in
  the audit doc.
- **A second, unrelated finding in the same `lemmy-import` command.**
  `app/cli.py:360` changes an existing user's password hash directly, bypassing
  `User.set_password()`, so `password_updated_at` is not stamped and that
  user's existing API tokens are not revoked by the import — even though eight
  other password-changing sites now do revoke them (see commit `27403474`).
  It was correctly excluded from that eight-site count, since it was never a
  `set_password()` call to begin with; it is still worth evaluating on its own
  list. `app/cli.py:377`, a second bypass in the same command flagged by the
  re-reviewer, turns out not to share the problem — a brand-new user has no
  prior credential or tokens to fail to revoke. See the note after F8 in the
  audit doc for both halves.

## Ratchet gotchas

- `percent_covered` is a **blended statement+branch figure**. This matters for
  any sub-project setting an interim floor below 100.
- `coverage.json` is gitignored and persists between runs. The documented
  command chains with `&&` so a failed test run cannot reach the ratchet; keep
  it that way.
- `--cov=app.module` (dotted) works; `--cov=app/module.py` (path) silently
  measures nothing.
- A floored module that vanishes from the report is a violation, not a pass.
  Missing or empty floors files are errors with exit 2 — the checker refuses to
  report success against no floors.

## Process notes

- Two test runs against the same tmpfs database corrupt each other's results.
  One suite run at a time per worktree.
- `./run_tests.sh --down` destroys the tmpfs database and forces a replay of
  ~269 migrations. Do not use it casually.
- `tests/test_activitypub_util.py` (3 tests) needs live network and a manually
  pre-seeded `rimuadmin` user. It predates this harness and is excluded from the
  documented commands. Suite totals that look 3 short are this.

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

## Sub-project 1b-ii: `app/utils.py` feed and query machinery

`docs/superpowers/specs/2026-08-26-coverage-utils-feed-design.md`. Covered
`get_deduped_post_ids`, `post_ids_to_models`, `instance_sticky_posts`,
`get_instance_stickies` and `possible_communities` -- the feed-assembly and
sort-chain functions. Raised `app/utils.py`'s floor from 60 to 67 (measured
`percent_covered` 67.1330%, rounded down). A fix round then closed two
previously undocumented gaps in `get_deduped_post_ids` (below), taking the
module to 67.2558% -- still rounds down to 67, so the floor did not move a
second time; both measurements had the floor-bites proof (68 fails, 67
passes) re-run against them. Five things from this sub-project outlive its
tests.

### 1. A sixth hand-carried figure, wrong on re-derivation -- this time in a brief

This sub-project's own design doc and task briefs stated all five target
functions were at 100% line and branch coverage except `possible_communities`'s
two documented-dead arms. Re-deriving it during the ratchet task showed
`get_deduped_post_ids` was NOT at 100%: two real gaps existed alongside its
three legitimately out-of-scope ones (the `hashtag` filter, the
anonymous-viewer private-community branch, and the unrecognized-`sort`
fallthrough). Nobody had decided to leave the other two uncovered -- they were
simply never noticed, because a "100%" claim from an earlier report was
carried into the brief instead of re-measured.

The two gaps, now closed in `tests/test_factories_feed.py`:

- **The empty-`community_ids` early return** (`app/utils.py:3792-3793`).
  `test_empty_community_ids_returns_an_empty_list_without_querying` calls with
  `community_ids=[]`; the discriminating mutation neutralizes the guard, which
  makes the next line index `community_ids[0]` on an empty list and raise
  `IndexError` rather than silently returning something else.
- **The Redis cache-HIT read path** (`app/utils.py:3794-3798`).
  `test_a_cached_result_id_is_served_without_reaching_the_database` primes a
  result_id's cached value (through `redis_double`) to something a live query
  could never produce, then asserts that STALE value comes back --
  discriminating a genuine cache hit from a test that would pass regardless of
  which path answered it.
  `test_an_authenticated_call_with_an_empty_result_id_still_writes_a_wasted_cache_entry`
  closes the branch's last arm and, in the same test, upgrades the design doc's
  suspected defect #1 from theoretical to **confirmed live**: two real call
  sites pass a literal `''` result_id (`grep -rn 'get_deduped_post_ids(' app/`
  --> `app/feed/routes.py:719`, `show_feed_rss`, and `app/topic/routes.py:230`,
  `show_topic_rss`). Neither route carries `@login_required`; both are
  reachable by an authenticated session anyway (no code path excludes one), so
  a logged-in browser opening either RSS URL performs the wasted write to a
  key literally named `''`. Reported, not fixed.

`get_deduped_post_ids` now carries exactly three documented-uncovered regions
(the three named above), unchanged by this fix round.
`post_ids_to_models`, `instance_sticky_posts` and `get_instance_stickies` are
100% statement and branch except `post_ids_to_models`'s own copy of the
sort-fallthrough branch. `possible_communities` carries its two
documented-dead branches, unchanged. Every one of the five functions is now
either 100% or carries a named, verified reason -- the standard this
sub-project's own verification criteria set.

### 2. The SQL blind spot is a limitation of every number this sub-project reports

Coverage.py measures the Python that *builds* a query, never the predicates
*inside* a query string. A branch that appends a SQL clause reads as covered
the instant it runs once -- regardless of whether the clause it appended
does anything correct, or anything at all, once the database evaluates it.

This sub-project found the concrete case: `get_deduped_post_ids`'s
`read_language_ids` filter appends `'(p.language_id IN :read_language_ids OR
p.language_id is null) '`. Three tests exercised the `IN (...)` half; none
exercised `OR p.language_id is null` -- a post with no language set was never
seeded -- and coverage read 100% throughout, because the Python `if` that
appends the whole string had both its arms taken. The gap was only found by
deleting the `OR ...` term and checking that no test failed; one didn't exist
yet, so nothing did. `tests/test_feed_display_preferences.py`'s
`test_a_posts_null_language_is_present` was added to close it, and the same
deletion now fails exactly that test.

So every "100% branch" result in this sub-project -- and the module floor
built from it -- means "every Python branch that appends a clause was
taken", **not** "every clause behaves correctly once appended". Future work
on `app/utils.py`'s remaining SQL-heavy functions (1b-iii) should not read a
green coverage number as license to skip enumerating the clauses inside each
query string by hand.

### 3. The two-direction mutation standard, and why one direction proves nothing about the other

A filter that gates *other people's* content needs two mutations, not one,
and they are not interchangeable:

- **Delete the clause.** This proves an ABSENCE test is load-bearing: with
  the clause gone, a thing that should have been filtered out now appears,
  and the absence test (and only it) fails.
- **Over-broaden the clause** (make it exclude everything, e.g. rewrite a
  predicate to `1=0`). This proves a PRESENCE test is load-bearing: with the
  clause excluding everything, a thing that should have stayed visible is
  now gone too, and the presence test (and only it) fails.

Deleting a clause can say nothing about whether the presence test is
load-bearing, because the clause usually sits inside a guard (`if
current_user.hide_nsfw == 1:`) that is FALSE in the presence-test's fixture
-- the deletion mutation is unreachable from that test's path entirely.
Three tasks in this sub-project (visibility filters, display preferences,
and the display-preferences fix round after review) shipped with only the
delete direction before both-direction mutation became the standard for
every filter gating another person's content; single-direction coverage was
then deliberately kept only for the seven display preferences, which gate
the viewer's own taste settings and whose failure mode is an annoyance
rather than a safety issue.

### 4. The wide/narrow discriminator, for a chain of `continue`s or `if`s

In a filter chain built from successive `continue`/`if` guards (
`get_instance_stickies`'s seven rules, `possible_communities`'s three dedup
checks), a rule that fires universally will short-circuit every downstream
presence test -- a wide mutant-detection blast radius is *expected* geometry
for a broad, early rule, not evidence of a problem.

But a wide blast radius is *also* exactly the symptom of hidden fixture
coupling between tests that should be independent. This sub-project found a
concrete instance: `User.hide_nsfw` and `User.hide_nsfl` default to `1`
(`app/models.py:984-985`), so any test file whose viewers don't explicitly
zero those two columns shares invisible state through the column default.
When `hide_nsfl`'s clause was over-broadened during mutation testing, **7 of
16 tests failed instead of 1** -- six other preferences' presence tests
collateral-failed because their viewers silently carried `hide_nsfl=1`.

The two failure counts look identical from the outside: "this mutation
failed more tests than expected." The discriminator is to run the NARROW
mutation (delete, not over-broaden) on the *same* rule and compare. A rule
that legitimately fires broadly still produces a narrow, single-test failure
under deletion (removing a guard's *effect*, not its trigger condition,
isolates exactly the rule that guard represents); coupled fixtures do not
self-correct that way, because the shared state that caused the wide result
under over-broadening is still shared under deletion. `possible_communities`
Task 7 used exactly this pairing on its `banned` predicate: over-broaden
gave 9 failures, delete gave exactly 1, on the same rule -- confirming
chain geometry, not coupling. The `hide_nsfw`/`hide_nsfl` case above was
caught the same way and then fixed by explicitly zeroing both columns on
every viewer not testing one of those two preferences.

### 5. Findings reported, not fixed

Carried forward as follow-up candidates, not acted on here per this
campaign's report-don't-fix rule:

- **`app/utils.py:4337` and `:4344`** (verified current against the file at
  commit time) are dead as within-loop duplicate filters inside
  `possible_communities` -- their `if c.id not in already_added:` guards can
  never take their False arm, because `CommunityMember`'s primary key is the
  `(user_id, community_id)` pair (`app/models.py:3366-3368`): at most one row
  per user per community, so `moderating_communities()` and
  `joined_communities()` can each return a given community at most once, and
  never both (their filters partition the same row's boolean space with no
  overlap). Their only live effect is the `already_added.add(c.id)` call each
  guards, which matters to the LATER "Others" loop's own (real, reachable)
  dedup check. Verified by instrumenting both dead arms with an assertion
  and attacking the claim four ways: a second `CommunityMember` row for the
  same pair (`IntegrityError`, confirming the PK), all nine combinations of
  `is_moderator`/`is_owner` including `None`, two same-title communities, and
  `possible_communities()` itself run over the adversarial fixture -- the
  instrumented arms never fired. This is a DRY target (three near-identical
  dedup blocks), not a deletion candidate: the guards stay, because the
  `.add()` they gate is not dead.
- **Task 5's spurious-ordering hazard.** An ordering test whose fixture rows
  are inserted in the SAME order the test expects the function to return
  them asserts nothing -- a function that silently ignored the `ORDER BY`
  entirely and just returned insertion order would still pass. This is only
  caught by running the ordering mutation (e.g. flipping `desc` to `asc`) in
  full-file context and confirming exactly the ordering test fails, never by
  reading the assertion in isolation. `tests/test_feed_sorts.py`'s pattern
  --seed `post_a` then `post_b`, assert the WINNING post comes first, i.e.
  the REVERSE of insertion order-- and `tests/test_possible_communities.py`'s
  `TestOrdering` --insert `zzzorderlast` before `aaaorderfirst`-- both exist
  because of this.

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

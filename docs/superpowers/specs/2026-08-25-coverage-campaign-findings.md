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
- `requests` — `pyld`'s default JSON-LD document loader (`jsonld.py:6547`),
  found by sub-project 4 (the inbox gate) when its LD-signature tests made
  real outbound HTTPS calls fetching JSON-LD contexts. Not named in this
  fixture's own docstring before that sub-project found it. See "A
  test-harness gap, not a production defect" under "Sub-project 4" below for
  the fix that sub-project applied, test-only, to its own two tests.

Those still reach the real internet from the test suite. **`app/nntp` is
entirely socket work and sits at 0% coverage** — that sub-project should expect
to design a socket-level block, and is deliberately last in the campaign order
partly for this reason.

respx consults routers in *registration* order, so the session router is asked
first and `http_mock` second. This is safe only because it registers zero
routes. **Never add a route to it** — a catch-all would silently override every
`http_mock` route in the suite.

### `redis_double` covers `get_redis_connection`, not `redis_client`

**Corrected 2026-08-29 (`coverage-inbox-gate`): stale.** This section
originally advised a sub-project needing `redis_client` isolation to widen
`redis_double` itself. `coverage-utils-feed` already did that widening, and
`tests/conftest.py`'s `redis_double` docstring documents it (the
"Also covered, as of the coverage-utils-feed sub-project: `app.redis_client`"
paragraph). Left as follows for the history; the advice in the last paragraph
no longer applies — do not re-widen the fixture.

`from X import Y` binds a new name in the importing module at import time, so
each binding must be patched separately. The fixture patches all four
`get_redis_connection` bindings.

`app.redis_client` — the global `create_app()` assigns — is read via
`from app import redis_client` in roughly fourteen modules
(`grep -rn 'from app import.*redis_client' app/` for the current set). ~~A
sub-project needing Redis isolation across `app/` should widen `redis_double`
rather than hand-roll its own.~~ `redis_double` now patches `app.redis_client`
directly (a single attribute, safe because every `from app import
redis_client` site does that import inside a function body, not at module
level) as well as the four `get_redis_connection` bindings above. The rate
limiter and Celery app are still built from `Config` at import time and are
outside any fixture's reach.

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

- **The empty-`community_ids` early return** (`app/utils.py:3800-3801`).
  `test_empty_community_ids_returns_an_empty_list_without_querying` calls with
  `community_ids=[]`; the discriminating mutation neutralizes the guard, which
  makes the next line index `community_ids[0]` on an empty list and raise
  `IndexError` rather than silently returning something else.
- **The Redis cache-HIT read path** (`app/utils.py:3802-3806`).
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

- **`app/utils.py:4352` and `:4353`** (verified current against the file at
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

## Sub-project 1c: `app/utils.py` link parsers

`docs/superpowers/specs/2026-08-27-coverage-utils-links-design.md`. Six tasks
covered `domain_from_url`, `remove_tracking_from_link`, `fixup_url`,
`rewrite_href` and `apply_feed_url_rules`, then fuzzed the first three and
ratcheted the floor. Re-measured (never carried forward) at **71.4776%**
blended `percent_covered`, up from 1b-ii's 67.6478%. This campaign has now had
seven hand-carried counts turn out wrong on re-derivation -- one of them
inside this very sub-project, when a reviewer told a previous implementer
that its brief's line numbers were stale after they had, in fact, already
been corrected. Rounded
down, the floor moved from 67 to 71, proved to bite in both directions: set to
72 the ratchet exits 1 naming `app/utils.py`; restored to 71 it exits 0 with
`All 3 module floors met.` Full detail, including the per-function coverage
figures (all five functions 100% statement and 100% branch on their current
line ranges) and the fuzz harness's iteration-count rationale, is in
`tests/README.md`'s "Sub-project 1c" section -- read that alongside this one.
Five things from this sub-project outlive its tests.

### 1. `domain_from_url`'s fix changes future attribution only

Before commit `81a3e40e`, `domain_from_url` (`app/utils.py:1442-1457`) read
`urlparse(url.lower().replace('www.', ''))` at line 1443 -- a blanket string
replacement over the WHOLE url, applied before parsing, that removed every
occurrence of the substring `www.` rather than a leading host label. An input
like `https://awww.evil.example/post/1` mangled to `aevil.example`, which is
also what the genuinely different host `aevil.example` mangles to -- two
unrelated hosts collapsing onto one `Domain` row, so banning either domain
banned both. The fix moves the strip to AFTER `urlparse`, scoped to a
`hostname.startswith('www.')` check, so only a leading host label is ever
touched (`tests/test_domain_from_url.py`'s `TestHostIsNotMangled` and
`TestLeadingWwwIsStripped` pin both directions).

**No migration, no backfill, and that is deliberate.** Existing `Domain` rows
created under the old mangling are not touched or reconciled -- an operator
who banned a domain whose row was created pre-fix keeps that row's behaviour
(and its collision with whatever other host used to mangle to the same name)
until a NEW row is created for the corrected hostname on some future post.
The design doc for this sub-project records why: backfilling would mean
deciding what happens to `DomainBlock` rows pointed at a mangled `Domain` id,
`post_count` totals split across what should be one host or wrongly merged
across what should be two, and posts already attributed to the mangled name
-- a data migration with its own risk, out of proportion to a fix whose job is
to stop new mis-attribution, not reconcile old records. Recorded here at
length for the same reason the design doc records it: so a later reader who
notices old `Domain` rows still look wrong does not mistake the absence of a
migration for an oversight this sub-project simply failed to do.

### 2. A second fix for the same defect class: `url_needs_archive`

Task 1's `domain_from_url` fix was not the only production change this
sub-project made. The campaign separately found and, with owner
authorisation, fixed an identical defect in `app/post/util.py`'s
`url_needs_archive` (commits `e0d08efc`, `3feab57c`; tests in
`tests/test_url_needs_archive.py`) -- the same `urlparse(url.replace('www.',
''))`-over-the-whole-string shape, before parsing, that `domain_from_url` had.

Its severity differs from `domain_from_url`'s: the return value feeds a
membership test against a hardcoded `paywalled_sites` list that drives a UI
affordance (whether to offer a `removepaywall.com` archive link), never a
`Domain` row used for attribution or ban enforcement. It still failed in both
directions -- `https://awww.nytimes.com/x` mangled to `anytimes.com`, a false
negative that withholds an archive link from a genuinely paywalled host, and
`https://nywww.times.com/x` mangled to `ny` + `times.com` = `nytimes.com`, a
false positive that hands an attacker-registered host a paywall-bypass link.
The fix mirrors `domain_from_url`'s idiom: parse first, then strip a
`startswith('www.')` prefix from the parsed hostname alone.

`tests/test_url_needs_archive.py` covers it with 11 tests, and its own report
is explicit that only 2 of them discriminate the fix:
`test_an_interior_www_does_not_produce_a_false_positive` and
`test_www_prefixed_paywalled_host_still_needs_archive`. The false-negative
test does not discriminate on its own -- it returns `False` both before and
after the fix, for the wrong reason pre-fix -- and the rest are regression
guards for adjacent behaviour (the two exemptions, falsy input, the bare
`except:`'s hostless-URL path). Worth preserving as stated rather than rounded
up to "11 tests cover the fix."

### 3. Coverage's blindness to strings applies to regexes and URL literals too

The SQL-predicate blind spot 1b-ii found (`OR p.language_id is null`,
completely untested while coverage read 100%, because coverage.py sees the
Python that BUILDS a string, never the content of the string once built)
recurs here in two different shapes, neither SQL:

- **A regex built by string interpolation.** `apply_feed_url_rules`'s
  private-mode branch (`app/utils.py:4502`) builds
  `r'^[a-zA-Z0-9_]+(?:/' + current_user.user_name.lower() + ')?$'` by
  interpolating `current_user.user_name`, unescaped, into a regex pattern.
  Task 5's coverage run showed this line and its branches at 100% -- the `if`
  that chooses this regex over the public-mode one was exercised -- with no
  signal at all that the regex it built was WRONG for any username
  containing a metacharacter. See "The username-regex probe" below for the
  concrete defect this let through.
- **A URL-matching literal.** `fixup_url`'s peertube branch checks
  `url[-25:][:3] == '/w/'` and its YouTube branch checks membership in a
  fixed `youtube_domains` list -- both are string/list literals coverage.py
  treats as opaque once the containing `if` is taken. A wrong or
  incomplete literal (an extra character in the slice check, a missing
  YouTube subdomain) would not lower the coverage number as long as the
  `if` itself is exercised both ways; only an explicit enumeration of the
  literal's own content, driven by real inputs, can catch that. Task 3's
  test suite enumerates every entry in `youtube_domains` for exactly this
  reason, rather than trusting the branch percentage.

Every "100% branch" figure this sub-project reports means "every Python
branch that runs was taken", never "every string or regex this code
constructs behaves correctly for every input" -- the fuzz harness (see below)
exists specifically because the coverage number cannot make that claim, and a
no-oracle property check can probe the string's behaviour where line coverage
cannot.

### 4. A bare `except:` blocks mutation testing

Found in Task 3, and worth stating as a general rule beyond this one
function: `app/utils.py:3126` and `:3128`, both in `fixup_url`'s peertube
branch, are bare `except:` clauses -- they catch EVERYTHING, including
`KeyboardInterrupt` and `SystemExit`, not just the exceptions the surrounding
code anticipates.

The concrete case that proved this is not a theoretical concern: with the
netloc guard at line 3117 (`if parsed_url.netloc in peertube_domains:`)
mutated wide (`if True:`, so every host takes the peertube branch), the
brief's own `test_an_unknown_host_makes_no_request` PASSED UNCHANGED --
18 of 18 tests survived the mutation. The cause: with the guard forced open
and no matching `http_mock` route registered for the now-attempted request,
`get_request` raised respx's own `AllMockedAssertionError` when it hit the
empty, autouse `block_outbound_http` router -- and the bare `except:` at 3128
swallowed that exception along with everything else, before it ever reached
an assertion. So the test's own docstring claim ("makes no request") was
never actually being verified by that test in isolation; the mutation that
should have made an unwanted request observable instead made the evidence of
that request disappear into the same catch-all that was supposed to handle
ordinary peertube-lookup failures.

The general lesson: a bare `except:` does not merely risk hiding a genuine
production failure (a network timeout, a malformed JSON response) behind a
silent `pass` -- it also makes the region it wraps RESISTANT TO MUTATION
TESTING, because any mutation whose failure signature happens to be an
exception -- including one raised by the TEST INFRASTRUCTURE itself, like
respx's assertion here -- is absorbed indistinguishably from the exceptions
the code was written to tolerate. Any future coverage or mutation-testing
work that encounters a bare `except:` should check explicitly whether its
own test-infrastructure fixtures (respx, moto, fakeredis) can raise inside
that block, not just assume the block's line coverage speaks for its
mutation resistance. Reported here, not fixed -- a change to `app/utils.py`
is out of this campaign's scope.

### 5. Guard-level vs. dispatch-level mutations

Found in Task 4, on `rewrite_href`'s four-rule `if`/`elif`/`elif`/`else`
chain (`app/utils.py:4968-4991`). The initial mutation-testing pass ran four
mutations, all incidentally guard-level, and drew the wrong general
conclusion from them: "each rule's body is independent, so a mutation cannot
bleed into another rule's tests." A fifth pairing -- deliberately run on a
DISPATCH condition rather than a guard -- showed that conclusion was true for
the four mutations tried, not true in general.

The corrected, general distinction:

- **Guard-level mutations** -- a check INSIDE an arm the dispatcher has
  already selected, such as the community rule's `not community.is_local()`
  test or the fallthrough's inner `if post_reply:` -- stay NARROW. By the
  time a guard runs, every other arm has already been foreclosed by the
  `if`/`elif`/`elif`/`else` chain itself, so mutating the guard can only
  change what that one already-selected arm does. Measured on the post
  rule's own body (both its return arms neutralized to `pass`, the dispatch
  condition at 4969 left untouched so the shape still matches): 2 of 11
  tests failed, both inside `TestPostRule` itself -- that rule has two
  match outcomes (slug / no-slug), so its own radius is 2, not 1.
- **Dispatch-condition mutations** -- the `if`/`elif` tests that decide WHICH
  arm runs at all, such as the URL-shape check at line 4969 -- go WIDE,
  because broadening one arm's condition steals inputs that would otherwise
  have reached a DIFFERENT, later arm's tests entirely, before any guard in
  either arm gets a chance to run. Measured: over-broadening the same post
  rule's dispatch condition (`4969 -> if True:`) failed 4 of 11 tests,
  spanning three of the four classes -- `TestCommentRule`,
  `TestCommunityRule`, and both `TestFallthroughElseRule` tests -- none of
  which touch the post rule's own body. The two failure sets are disjoint:
  zero overlap between the wide set and the narrow set above, confirming
  the wide failures are structural short-circuiting (the always-true
  condition steals every URL before later arms are ever reached), not
  hidden fixture coupling.

Radius alone is not a signal of a problem (1b-ii's wide/narrow discriminator
already established that a wide radius can be correct, expected chain
geometry rather than fixture coupling). What discriminates a correct wide
result from a coupling bug is pairing: run BOTH a wide-shaped mutation
(here, a dispatch condition) and a narrow-shaped one (a guard) on the SAME
rule, and confirm the radius each produces matches its class -- wide for the
dispatch condition, narrow for the guard. This generalizes 1b-ii's
wide/narrow standard for `continue`-chains to `if`/`elif` dispatch chains: the
pairing should be run on a rule's DISPATCH CONDITION whenever the chain has
more than one arm after it, not only on the guards inside each arm's body.

### The username-regex probe (Task 5)

Established with executed commands, not by inspection alone, that a regex
metacharacter CAN reach `current_user.user_name` and, when it does,
`apply_feed_url_rules`'s interpolated regex mis-validates -- a real defect,
reported and not fixed.

Two username-validation paths exist and only one of them is a real gate:

- `RegistrationForm.validate_user_name` (`app/auth/forms.py:55-60`) checks
  both `'@' in user_name.data` and `re.match(r'^[a-zA-Z0-9_]+$',
  user_name.data)`. This closes the self-registration path entirely -- no
  metacharacter can reach a `User` row created this way.
- `AddUserForm.validate_user_name` (`app/admin/forms.py:308-319`), the
  admin-panel user-creation path, checks ONLY `'@' in user_name.data`. No
  charset restriction at all. An instance admin can create a user with any
  username containing no `@`, including regex metacharacters, and that user
  then logs in and becomes a fully valid `current_user` for
  `apply_feed_url_rules`.

`tests/test_apply_feed_url_rules.py`'s
`TestUsernameRegexMetacharacterProbe::test_username_regex_metacharacters_are_not_escaped`
demonstrates the consequence directly against `apply_feed_url_rules` (driving
only the DB state the admin form's HTTP path would produce, not that path
itself): a user named `a.b` is built, a private-mode url `myfeed/aXb` is
submitted -- NOT that user's real `<feed>/<username>` -- and
`apply_feed_url_rules` returns `True`, wrongly validating it.
`r'^[a-zA-Z0-9_]+(?:/' + current_user.user_name.lower() + ')?$'` treats the
`.` in `a.b` as "match any character" rather than a literal dot, so `aXb`
(and any other single-character substitution for the dot) satisfies the
optional suffix group that was meant to require the caller's exact username
-- letting user `a.b` claim a feed url in what reads as a different user's
namespace. `re.escape()` on the interpolated segment would close this;
per this campaign's rule, it was not applied here.

### `domain_from_url` has a second consumer, which matters for mutation runs

`tests/test_link_parsers_fuzz.py` imports and calls `domain_from_url` directly
-- it is one of the three functions the fuzz harness targets -- making it a
second, independent test file exercising that function alongside
`tests/test_domain_from_url.py`. The whole-branch review's own mutation run
against `domain_from_url` found 3 failures across 11 files, and the fuzz file
was one of them: proof its property check discriminates rather than merely
restating the implementation, and proof that a future mutation run scoped only
to `tests/test_domain_from_url.py` will under-count `domain_from_url`'s real
kill rate. Include the fuzz file in any future mutation run against this
function.

### Findings reported, not fixed

Carried forward as follow-up candidates from this sub-project, consistent
with the report-don't-fix rule:

- **`app/utils.py:3126,3128`** -- the bare `except:` clauses described at
  length above, also catching `KeyboardInterrupt` and `SystemExit`.
- **`app/utils.py:4984-4989`** -- in `rewrite_href`'s fallthrough `else`
  arm, `post = Post.get_by_ap_id(url)` is used only as a null check to
  decide whether to fall through to a `PostReply` lookup. When a `Post` IS
  found, the branch does nothing with it and returns the url unchanged --
  a full entity query serving only as an existence test, with the matched
  entity itself discarded.
- **`app/admin/forms.py:308-319` + `app/utils.py:4502`** -- the
  username-regex probe above.
- **`app/feed/forms.py:91`** -- a stray `print(f"input_communities:
  {input_communities}")` left in production code.
- **`test_api_community_subscriptions`** -- an unrelated, pre-existing
  order-dependent flake noted while running this sub-project's full-suite
  measurements: it fails in some full-suite orderings and passes in
  isolation. Not investigated further here (out of scope), but recorded so
  a future red run of exactly this test is recognised rather than
  re-diagnosed from nothing.
- **A crash class this sub-project's fuzz harness found**
  (`tests/test_link_parsers_fuzz.py`): none of `domain_from_url`,
  `remove_tracking_from_link` or `fixup_url` catches the `ValueError` that
  Python's own `urlparse` raises for certain malformed netlocs -- an
  unbalanced IPv6 bracket, a host that fails urllib's NFKC
  homograph-confusability check, or (for `domain_from_url` only, one DB
  round trip further) a hostname containing a literal NUL byte, rejected by
  the Postgres driver rather than by `urlparse` itself. A submitted post
  link containing any of these raises, uncaught, straight out of
  application code that has no reason to expect a raw `urllib` exception
  from parsing a string. The committed fuzz test catches this specific,
  now-documented crash class so it can keep searching on every future run
  without turning the suite red on a known, reported finding.

### YouTube's URL formats are observed behaviour, not a specification

`fixup_url`'s YouTube matrix (`/shorts/<id>`, `/watch?v=<id>`, `/playlist`
with `list` in the query, `/post/<id>`, and the five recognised hostnames
`www.youtube.com` / `m.youtube.com` / `music.youtube.com` / `youtube.com` /
`youtu.be`) is pinned in `tests/test_fixup_url.py` as OBSERVED behaviour --
derived from the shapes the production code already handles -- not as an
authoritative source. YouTube publishes no specification for its URL
conventions and can change them without notice; WHATWG's URL Standard and
RFC 3986 govern the actual parsing underneath (`urlparse`, `parse_qs`), and
those are the only parts of this matrix with a real specification behind
them. A future YouTube URL shape that this matrix does not recognise is
expected, not a coverage gap in the sense the rest of this document uses
that phrase.

## Sub-project 2a: `app/activitypub/util.py` actor and object ingestion

`docs/superpowers/specs/2026-08-27-coverage-activitypub-ingest-design.md`. Eight
tasks covered the six functions through which a remote peer's ActivityPub
documents first become rows in this database: `ensure_domains_match`,
`find_community`, `remote_object_to_json`, `verify_object_from_source`,
`find_flair_or_create`, and `actor_json_to_model` (split three ways, one task per
actor branch -- Person/Service, Group, Feed). Eight test files, 290 tests, and
the module's **first** coverage floor, set at 35.

This is the campaign's first sub-project on a file it does not attempt to cover
whole. `app/activitypub/util.py` is far too large for that, so the unit of work
was the function, not the module, and the floor reflects the module. All six
target functions are fully covered; the floor is 35 because everything else in
the file is still untested. A future sub-project raising it should say which
functions it added, not just which number went up.

### 1. The two suspected defects the spec named in advance

Both were named before any test was written, and in **both cases the call-site
analysis reversed the predicted severity**. That is the transferable finding, not
either defect: a comparison that looks like a security boundary when you read it
in isolation may be one that no caller can drive, and the only way to tell is to
enumerate every caller and trace where each compared string comes from. Reading
the comparison tells you what it does; reading the call sites tells you what it
is worth.

#### `netloc` instead of `hostname`

`ensure_domains_match` and `verify_object_from_source` both compare
`urlparse(...).netloc` strings rather than `.hostname`. Probe, re-run against
this checkout for this write-up:

```
https://peer.example/x                    netloc='peer.example'                hostname='peer.example'
https://peer.example@attacker.example/x   netloc='peer.example@attacker.example' hostname='attacker.example'
https://peer.example:8443/x               netloc='peer.example:8443'           hostname='peer.example'
https://PEER.example/x                    netloc='PEER.example'                hostname='peer.example'
```

**The userinfo row is the one that looks dangerous and is not.** `hostname` is a
pure function of the `netloc` string itself -- strip userinfo up to the last `@`,
strip a trailing `:port`, lowercase -- so two inputs with an **equal** `netloc`
can never have **different** `hostname`s. No input makes two genuinely different
real hosts compare equal under a netloc test that would have compared unequal
under a hostname test. The "userinfo lets a peer impersonate another host"
reading does not hold, and it cannot be made to hold by a peer that controls both
strings, because such a peer can simply make them identical outright. What
userinfo *does* do is ride along unstripped into whatever downstream code or log
re-displays the extracted "domain", which is a readability concern, not a
boundary bypass.

**The port row is the real divergence, and it runs the other way.**
`peer.example:8443` and `peer.example` are the same host and different strings,
so the comparison **falsely refuses** a peer that is inconsistent about the port
across the fields being compared. (A peer that carries the same port everywhere
compares equal and passes -- an earlier draft of the Task 4 report had this
premise inverted and claimed the refusal was total for any ported peer. It is
not. It requires inconsistency, and once that inconsistency exists it is
deterministic, because URI generation is.) This is an availability defect.

Call sites, and why they give the two functions different severities:

- `ensure_domains_match` has exactly one call site, inside `process_inbox_request`
  in `app/activitypub/routes.py`. The dict it receives is the raw inbound POST
  body, or the announced object inside it, or -- in the one branch that replaces
  it -- the peer's own HTTP response to a fetch aimed at the domain the peer's
  own `actor` field names. **The peer controls both compared strings on every
  path.** That makes the false reject genuinely peer-triggerable, but its blast
  radius is one activity declined.
- `verify_object_from_source` is materially worse, and this is a severity
  *difference between two instances of the same defect*, which is the kind of
  thing a defect list flattens if you let it. That function is the gate deciding
  whether an `Announce`-wrapped object is believed **at all**; a refusal returns
  `None` and the announced content is dropped, not merely declined once. It has
  two netloc comparisons, one before the fetch and one after, and the pre-fetch
  one refuses without ever contacting the peer -- so there is no response, no
  status code and no transport error for anyone to diagnose from.

The diagnosis problem compounds it. All ten of `verify_object_from_source`'s
refusal paths return a bare `None`, and the caller logs every one of them as the
single string `'Could not verify unsigned request from source'`. The refusal *is*
recorded -- an earlier draft of the Task 4 report wrongly said nothing logged it
-- but an operator can see that verification failed and cannot see which of the
ten causes fired. A port mismatch, a fetch failure, a malformed document and an
`attributedTo` of the wrong type are indistinguishable in the log.

One rewrite (`urlparse(x).hostname` on both sides) closes the port false-reject
in both functions.

#### The substring server gate

`actor_json_to_model` opens with `if server not in activity_json['id']: return
None` -- a substring test against the whole id string, not a host comparison.
Probe, re-run for this write-up, with `server = 'good.example'`:

```
https://good.example/u/alice                passes    real host = good.example
https://good.example.attacker.net/u/alice   passes    real host = good.example.attacker.net
https://attacker.net/u/x?ref=good.example   passes    real host = attacker.net
https://other.example/u/alice               REJECTED  real host = other.example
https://GOOD.EXAMPLE/u/alice                REJECTED  real host = good.example
```

Rows two and three are the weakness: a suffix-extended host and a query parameter
both satisfy a substring test. Row five is a second, separate defect found while
writing the tests and not predicted by the spec -- the test is **case-sensitive**
as well as host-blind, so a peer publishing its own id with an upper-cased host is
rejected outright. Same false-reject shape as the port case above.

**The call-site analysis is what settles the severity, and it deflates it.**
There are five call sites, and in every one `server` is derived locally from the
address PieFed is resolving -- `urlparse(...).netloc` of a URL being fetched, or
the host half of a `name@host` handle being webfingered, or (in the alpha API's
object resolver) `urlparse(query).netloc.lower()`, in a function that re-enters
itself with the document's own `id` as the new query whenever the two disagree,
so by the time it reaches `actor_json_to_model` they agree exactly. **A peer never
supplies `server`.** The predicted "impersonate any instance" reading therefore
does not hold: a peer cannot choose the string it is checked against, so it cannot
use this to be admitted as an actor of an instance it does not control.

What a peer **can** do, with the cooperation of the host being fetched, is mint a
row whose `ap_profile_id` and `ap_public_url` point at a host that is neither the
host PieFed fetched from nor the host recorded in the same row's `ap_id`,
`ap_domain` and `instance_id` -- cross-host actor smuggling. The resulting `User`
claims to be `alice@good.example` while the column every inbound activity is
matched against lives on `attacker.net`. One amplifier is worth recording for
whoever fixes it: on the webfinger paths the actor document is fetched from the
`href` in the peer's own webfinger response, which that peer chooses freely, so
`server` is pinned to the handle's host while the fetch can be redirected
anywhere. The substring gate is the only thing left tying the two together, and a
substring test does not tie them.

The same one-line rewrite (`urlparse(id).netloc.lower() == server`) closes both
the smuggling weakness and the case-sensitivity false reject.

### 2. Partially-applied ingest -- three defects that share one shape

Three of this sub-project's nineteen defects are the same bug wearing different
clothes, and they are far more legible as a group than as three entries in a
list. In each, a peer-supplied document causes a row to be **committed** and then
causes an exception to escape the function. The caller gets an exception instead
of a return value, so it has no way to know a row was written; the row survives
because it is already committed and no rollback covers it. The peer ends up
half-ingested, and nothing records that.

- **`actor_json_to_model`, Group branch, legacy flair.** The `lemmy:tagsForPosts`
  loop reads `flair['display_name']` unguarded, unlike the four optional keys
  that follow it, each of which sits behind an `in` test. That loop runs *after*
  the community is added and committed. A legacy tag entry with no
  `display_name` therefore leaves a persisted `Community` with no flair and
  raises `KeyError` at the caller. Pinned by a test asserting the `KeyError`,
  a `Community` row count of 1, and a `CommunityFlair` row count of 0.
- **`actor_json_to_model`, Feed branch, the following collection.** Entries of
  the fetched `/following` collection are resolved and appended without checking
  the resolver's return, and the later loop builds a `FeedItem` from each
  entry's `.id`. That second loop runs *after* the feed is added and committed,
  so an entry the resolver rejects leaves a persisted `Feed` with no `FeedItem`s
  and raises `AttributeError` at the caller. Pinned the same way.
- **`find_flair_or_create`, reached from `refresh_community_profile_task`.** The
  ap_id backfill block reads `flair['id']` unconditionally -- again, unlike every
  other optional read in that function. The caller commits the community's
  refreshed profile fields and *then* runs its `lemmy:tagsForPosts` loop, which
  builds each dict from `display_name` plus optional colour and blur keys and
  never an `'id'`. That caller's session comes from `get_task_session()`, which
  leaves `autoflush` at its default of on, so two entries in the same
  peer-supplied list sharing one `display_name` are enough: the first call adds a
  flair row with a null `ap_id`, autoflush makes it visible to the second call's
  query, and the second call finds it, sees the falsy `ap_id`, and reads the key
  that is not there.

That last one is also the sub-project's sharpest example of **the same defect
being reachable from one caller and not another, for a reason that is nowhere
near the defect.** `actor_json_to_model` calls `find_flair_or_create` with no
session argument, so it gets `db.session`, and this application constructs
Flask-SQLAlchemy with `session_options={"autoflush": False}`. Without autoflush
the first call's pending insert is invisible to the second call's query, so the
same-call collision never forms. The reachability of a `KeyError` in one function
turns on a session option set in the application factory. A defect list that
records only "unguarded key read" loses that entirely, and the next person
re-derives it.

The general shape to look for when reading ingestion code: find every commit, and
ask what runs after it. Anything that can raise after a commit and before the
return is a partially-applied ingest waiting for a malformed document.

### 3. Dead code that no coverage number can surface

The Feed branch builds `ap_following_url` from a conditional expression whose
else arm supplies `None` when the document has no `following` key. **That arm can
never be taken.** The same key is read *unconditionally*, earlier in the same
branch, to fetch the feed's following collection over HTTP, so a document lacking
it has already raised `KeyError` before the constructor runs. Pinned by a test
that asserts exactly that ordering.

The reason to give this its own section is the *mechanism by which it stayed
hidden*, which generalises well beyond this file:

- **Statement coverage cannot see it.** The line executes on every call. It reads
  100% covered and always will.
- **Branch coverage cannot see it either.** coverage.py emits **no branch arc for
  a conditional expression**. There is no arc to be missing, so the branch number
  is not merely uninformative here -- it is structurally incapable of carrying
  the information. The whole-function measurement for `actor_json_to_model` is
  177 of 178 arcs, and not one of those 178 has anything to say about this
  expression or about any of the other conditional expressions in the three actor
  branches, of which there are many.
- **What found it was the discipline, not the tooling.** Someone enumerated the
  branch's conditional expressions, sat down to write the absent-side test for
  each, and discovered that for this one there was no absent side to write.

This is the campaign's clearest argument for present-and-absent testing as a
standing requirement rather than a nice-to-have. Any function with conditional
expressions has, in the coverage report, a region about which the report says
nothing at all while looking fully green. The only instrument that reaches into
that region is a human required to construct both inputs, who is then forced to
notice when one of them cannot be constructed.

The corollary for reading any number this campaign produces on a file with many
conditional expressions: arcs are evidence about `if`/`elif` statements and `for`
loops; statements are evidence that no line is dead to the suite; neither is
evidence about a conditional expression. Present-and-absent test pairs are, and a
two-direction mutation on the guard is what confirms a pair discriminates rather
than merely executing both spellings.

### 4. Bare `except:` and mutation resistance -- what was observed, not assumed

Finding 4 of sub-project 1c states the general hazard: a bare handler swallows
respx's own `AllMockedAssertionError`, so a test whose entire claim is "this makes
no request" passes under a mutation that makes the request. This sub-project went
looking for live instances in the two network functions,
`remote_object_to_json` and `verify_object_from_source`, which carry two bare
`except:` clauses each. The result was **more mixed than the general principle
predicts**, and the details are the useful part.

**In both network functions the bare handlers wrap only the `.json()` parse, not
the fetch.** The fetch is guarded by `except httpx.HTTPError`. respx's error is
an `AssertionError` subclass, which matches none of that, and matches none of
`get_request`'s five handler clauses either (`httpx.InvalidURL`, `ValueError`,
`httpx.ReadError`, `httpx.HTTPError`, `httpx.StreamError` -- enumerated by
reading the function, after an earlier draft listed only three). So an unwanted
fetch does still error the test, and "register no route, let the suite-wide
blocker prove nothing was fetched" is a valid proof in those two files. All the
mutations aimed at those regions were killed.

**But that is a property of how the tests were built, not of the handlers.** The
`remote_object_to_json` tests deliberately serve the *wrong* branch a body that
parses as valid JSON carrying a distinguishing marker, so an over-broadening
mutation is caught by an observably wrong return value rather than by an
accidental raise the bare handler could have absorbed. Shaped the other way, the
same mutations would have been swallowed. The Feed task hit exactly that mirror
image and had to fix it: two `status_code == 200` guards mutated to `True`
**survived the first battery**, because the test helpers served JSON bodies on
non-200 responses, so the mutated code's `.json()` succeeded and produced the
same empty list the unmutated code produces by skipping the block. Serving a
plain-text body on any non-200 status killed both. Those two mutations had been
*named as killed* in docstrings at the moment they were surviving; running every
mutation you name is what caught it.

**The live instance of the 1c hazard in this sub-project is somewhere else
entirely:** `make_image_sizes_async` wraps its `get_request` in a bare
`except: pass`. Four mutations across the Group and Feed tasks -- deleting the
icon and image resize guards -- cannot be killed by any assertion, because the
only observable effect of those guards is the fetch, and the bare handler eats
the harness's own block. They are killed only by `http_mock`'s
`assert_all_called=True` turning "a registered route was never fetched" into a
teardown error. That is a real kill, but it is the only signal available, and it
is also why the Person task's negative image-caching test can assert which `File`
rows exist and cannot assert that no fetch happened.

The rule to carry forward, sharper than 1c's: when a bare `except:` is in scope,
determine whether it wraps the **call** or only the **parse**, and check whether
your own fixtures can raise inside it. Both answers occur in this one file.

### 5. Patching `time.sleep(3)` is not a mock of the code under test

Both network functions retry twice and sleep three seconds before the second
attempt. Tests patch that out, and it is worth writing down why that is not the
thing this campaign otherwise forbids.

The patch replaces the clock. It chooses no branch, supplies no return value, and
is never asserted against; every assertion remains on the document the function
returns. Every mutation against the retry structure is still caught. A test that
patched `get_request` would be mocking the code under test; a test that patches
`sleep` is removing three seconds of real time from a deterministic path.

Two separate bound names must be patched, and missing the second is the trap:
`app/activitypub/util.py` does `import time` and calls `time.sleep(...)`, so
patching the attribute on the shared module object reaches it -- but `app/utils.py`
does `from time import sleep`, a different name in a different module, unreached
by that patch. It matters because `get_request` has its own internal retry
sleeping a random 3-10 seconds, nested inside the outer one, so a single "transport
error twice" case can chain up to four unpatched sleeps.

### 6. The defect register: 20 found — 15 fixed, 5 still open

**Read this line first: five of the twenty are still true of the code.** They are
**D6, D8, D18, D19 and D20**. The other fifteen were fixed on branch
`fix-ap-ingest-defects` (sub-project 2c, section "Sub-project 2c" below) and each
carries its commit in the Status column of the table. If you are looking for work
here, the five open rows are the whole list; the fifteen fixed rows are history
and re-investigating one is wasted effort.

The two that are open for reasons other than "nobody got to them yet": D8 is
cosmetic and nothing triggers it, and D18 and D19 are dead code, whose removal is
a behaviour-preserving edit nobody has authorised. D6 and D20 are genuine
unfixed defects.

Derived by extracting each task report's own defects section and counting its
entries, excluding the two entries those reports themselves label "(Observation,
not a defect)":

```bash
cd .superpowers/sdd/2026-08-27-coverage-activitypub-ingest
for f in task-{1,2,3,4,5,6,7,8}-report.md; do
  awk '/^## Defects? /{on=1;next} /^#{1,2} [^#]/{on=0} on' "$f" \
  | grep -E '^(### )?[0-9]+\. ' | grep -vc 'Observation, not a defect'
done
```

That yields 17. Two more are carried in the singular-heading reports that state
one defect in prose rather than as a numbered list -- Task 1's ("## Defect
reported (not fixed)") and Task 5's ("## Defect found (reported, not fixed)").
Task 3 found none new. **17 + 2 = 19.**

D20 is not in that count and cannot be: it was found during the whole-branch
review that closed the sub-project, after the last task report was written. It
is described in full in section 8 below.

| # | status | function | defect |
|---|---|---|---|
| D1 | fixed `9666112f` | `ensure_domains_match` | compares `netloc`, not `hostname`; falsely refuses a peer inconsistent about its port |
| D2 | fixed `9aa44e45` | `find_community` | `KeyError` reading `type` from an object that has none; reachable from the `Add`/`Remove` inbox handlers and from the search-driven remote post resolver |
| D3 | fixed `9aa44e45` | `find_community` | `AttributeError` calling `.startswith` on a non-string element of a `cc`/`to`/`audience`/`target` list; reachable from every call site, including the one guarded against D2 |
| D4 | fixed `2a8a0a98` | `verify_object_from_source` | the same `netloc`-not-`hostname` comparison, in both guards, with a worse consequence (see section 1) |
| D5 | fixed `2a8a0a98` | `verify_object_from_source` | two bare `except:` clauses around the JSON parse; they will swallow any error `.json()` raises, including a programming error inside httpx |
| **D6** | **OPEN** | `verify_object_from_source` | the signed-retry branch dereferences the `Site` row without a null check, so a missing row raises `AttributeError` instead of returning `None` like every other failure path |
| D7 | fixed `2a8a0a98` | `verify_object_from_source` | ten distinct refusal paths collapse into one undifferentiated log message |
| **D8** | **OPEN** | `verify_object_from_source` | `object` shadows the builtin throughout the function body (cosmetic) |
| D9 | fixed `539f0b81` | `find_flair_or_create` | ap_id backfill reads `flair['id']` unguarded -- **partially-applied ingest** via `refresh_community_profile_task` (see section 2) |
| D10 | fixed `43cba4b2` | `actor_json_to_model` | the `server` gate is a substring test, not a host comparison (see section 1) |
| D11 | fixed `43cba4b2` | `actor_json_to_model` | that same gate is case-sensitive, so an upper-cased host in a peer's own id is rejected |
| D12 | fixed `07089cf1` | `actor_json_to_model`, Group | the branch has no `except KeyError` at all, unlike Person/Service; five unconditional keys raise straight out of the function, and its inbox expression has no fallback where Person's ends in an empty string |
| D13 | fixed `539f0b81` | `actor_json_to_model`, Group | legacy flair entry without `display_name` -- **partially-applied ingest** (see section 2) |
| D14 | fixed `07089cf1` | `actor_json_to_model`, Feed | the branch has no `except KeyError` either; same asymmetry as D12 |
| D15 | fixed `e283764c` | `actor_json_to_model`, Feed | the first owner is indexed out of the owners list with no guard: three distinct crashes (non-200 collection, empty collection, an entry the resolver rejects), all before the commit |
| D16 | fixed `539f0b81` | `actor_json_to_model`, Feed | the following collection's rejected entries -- **partially-applied ingest** (see section 2) |
| D17 | fixed `e283764c` | `actor_json_to_model`, Feed | a document with neither `attributedTo` nor `moderators` sends `None` into `get_request`, which raises `httpx.HTTPError` out of the function; the failure's exception *type* differs depending on whether `DEBUG` is on |
| **D18** | **OPEN** | `actor_json_to_model`, Feed | `ap_following_url`'s else arm is dead code (see section 3) |
| **D19** | **OPEN** | `actor_json_to_model`, Feed | the post-commit re-fetch guard protects nothing -- always true, and the statement after the block it guards dereferences the same value anyway |
| **D20** | **OPEN** | `find_flair_or_create` | the update path writes the peer's flair name back **unstripped**, where the lookup and create paths both strip it; a peer sending a padded name gets one value on the first delivery and a different one on the second (see section 8) |

**Correction to D13's status: `539f0b81` fixed half of it, and the guard it
added raised on the other half.** The row above still reads `fixed 539f0b81`,
per this table's convention that corrections are recorded under their rows
rather than quietly applied — so read the row and this note together. D13 is
**completed by `ed88341f`** on branch `fix-ingest-shape`, under separate owner
authorisation.

- **What `539f0b81` covered.** `if 'display_name' not in flair: continue` in
  `actor_json_to_model`'s legacy `lemmy:tagsForPosts` loop. That turns away a
  **dict missing the key** — the `KeyError` the row describes — and it was
  reviewed twice and marked fixed on that basis.
- **What it missed.** Every entry that is not a dict at all. `'display_name'
  not in 5` is itself `TypeError: argument of type 'int' is not iterable`, so
  the guard *raised on the case it was meant to catch*; and for a string the
  same expression is a legal substring test, so an entry like
  `'{"display_name": "Doubled"}'` passed the guard and died one line later on
  `TypeError: string indices must be integers`. Both were reproduced against
  the code as `539f0b81` left it: the exception escaped `actor_json_to_model`
  with the `Community` row already committed and no flair — the
  partially-applied ingest unchanged. So the *shape* survived its own fix.
- **What completes it.** `if not isinstance(flair, dict) or 'display_name' not
  in flair:`, matching D26's guard in `refresh_community_profile_task` and
  D30's in the `tag` loop one arm above. The asymmetry was the bug: three loops
  doing the same job, one of them guarded differently.
- **Why this took a second pass to notice.** A compound guard's two halves can
  both look exercised while one is vacuous for the chosen data — D30 hit this
  first, with string entries for which the membership half alone sufficed. The
  completing tests use a string that *contains* `display_name` (only
  `isinstance` refuses it), an integer, and a dict missing the key (only
  membership refuses it), so each half is killed on its own. All four mutations
  — drop either half, delete the guard, broaden to `if True:` — were run.

The general lesson, and the reason this is filed as a correction rather than a
tidy-up: **a membership test is not a type test.** `KEY not in entry` is a call
into `entry`, so using it as the guard against a malformed `entry` is circular
— it raises for a non-container and silently answers the wrong question for a
string.

**The same gap was searched for in the rest of the module and no third instance
was found.** After `ed88341f`, every `KEY not in X` where `X` is peer-supplied:

```bash
grep -nE "^ +(el)?if .*(\"|')[A-Za-z:_]+(\"|') not in " app/activitypub/util.py
```

That prints six lines. Three are the three flair-loop guards, and all three now
carry `isinstance`; a fourth, `refresh_community_profile_task`'s outer
`"tag" not in activity_json` test, is a membership test on the *document*. The
remaining two — `actor_json_to_model`'s opening `'type' not in activity_json`
and `process_report`'s `'summary' not in request_json` — are guards on a whole
request document rather than on an element of a peer-supplied *list*.

They are safe, but **not for the same reason, and one of them not for the reason
first given here.** `process_report`'s is safe because its caller subscripts
`core_activity['type']` and `['object']` before the call, so the value is a
mapping by the time the guard runs. `actor_json_to_model`'s is *not* safe on
that argument: `fetch_remote_actor_data` returns `response.json()` unvalidated,
so a peer serving `5` at its actor URL reaches that guard with an integer and
raises `TypeError` from inside it — the same defect D13 had. It is safe for a
different reason: it is the function's **first statement, before any write**, so
the failure is a plain exception rather than a partially-applied ingest.

That distinction is the one worth carrying. A bare membership guard on
peer-supplied data is always capable of raising; whether that matters depends
entirely on whether anything has been committed by the time it runs.

The Status column was derived from git rather than from the fixing sub-project's
prose ledger, by listing every commit on `fix-ap-ingest-defects` that touched the
module and reading each one's hunk headers, which carry the enclosing function
name:

```bash
git log --reverse --format='COMMIT %h %s' -p -U0 blentz..HEAD -- app/activitypub/util.py \
  | grep -E '^COMMIT |^@@'
```

That prints eight commits, one of which (`7d74fd51`) is a docstring correction in
`host_of` and fixes no register entry; the other seven are the fifteen fixes.
Where one commit closes several rows it is because the defects share a function
and were briefed as one task -- D4/D5/D7 in `verify_object_from_source`, D2/D3 in
`find_community`, D9/D13/D16 across the three partially-applied-ingest sites.

Two qualifications on how this register is often summarised. **They are not all
peer-triggerable.** D8 is cosmetic and nothing triggers it; D6 needs a missing
`Site` row, which is a deployment state a peer reaches but does not create. The
other seventeen are driven by a peer-supplied document. **And numbering is not
stable across summaries** -- the Task 9 brief refers to the `ap_following_url`
finding as "defect 17", which is what you get if you drop the cosmetic D8. It is
D18 here. Cite these by function and behaviour, not by ordinal.

That second warning came true, and it is worth reading the way it happened.
Sub-project 2c found five new defects across its tasks and its ledger
provisionally called them D21–D25; sub-project 2b, running its own closing task,
committed D21–D24 for the `netloc` reads. Two tasks each reached for "the next
free number" and got the same answer, because each was reading a register that
did not yet contain the other's entries. The committed numbers won and the five
became D25–D29.

**How to allocate a number so this cannot happen again.** The collision was not
caused by careless counting; it was caused by there being no single place that
says which numbers are taken. So there is one now, and it is this file:

- **A defect number exists only once it is a committed row in this document.**
  A number written in a task report, a brief or a working ledger is a proposal,
  not an allocation. Two proposals can hold the same number without either being
  wrong.
- **Allocate at the point of writing the row, not at the point of finding the
  defect** -- read the highest number in the file, take the next one, and commit
  the row in the same change. The window in which a number can collide is then
  the length of one commit rather than the length of a sub-project.
- **Tasks that find defects report them by function and behaviour and leave the
  numbering to whichever task files them.** That is the same rule as "cite by
  function and behaviour", applied to the writing side rather than the reading
  side.
- **The allocation ledger, kept current:** D1–D20 sub-project 2a, D21–D24
  sub-project 2b, D25–D29 sub-project 2c, D30–D33 sub-project 2c's whole-branch
  review. D34–D40 sub-project 3. D41–D46 sub-project 4 (the inbox gate).
  D47 sub-project 4's whole-branch review (the dict-actor allowlist bypass;
  taken here, in the change that files the row). D48 the follow-on audit of
  `instance_allowed`/`instance_banned`'s other call sites, which D47's row named
  as its deeper half and left unexamined.
  **Next free number: D49.** If you take it, say so here in the change that
  takes it.

Two entries in the reports were deliberately **not** counted as defects, and are
recorded here so nobody re-files them: the Group and Feed branches both ignore
the `cache_remote_images_locally` setting that the Person/Service branch honours
on the same two image-resize calls. That asymmetry may well be intentional. It is
noted in the relevant test docstrings.

### 7. The first floor, and what 35 means

Measured from the whole suite, which was green at the time: **2528 passed, 3
skipped, 0 failed.**

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import json; d=json.load(open('coverage.json'))
s=d['files']['app/activitypub/util.py']['summary']
print(s['percent_covered'], s['num_statements'], len(d['files']['app/activitypub/util.py']['missing_lines']))
"
```

`percent_covered` came back **35.06818181818182**, over 2816 statements with 1778
missing. Floor set at **35**, rounded down, as an added line in
`coverage_floors.ini`; the three existing floors were left untouched.

That figure is the **blended statement+branch** number (`.coveragerc` sets
`branch = True`, so it is blended whether or not `--cov-branch` appears on the
command line), and it is what `tests/check_coverage_floors.py` reads. It is not a
statement percentage: statements alone are 1038 of 2816 and branches alone are
505 of 1584, and 1543/4400 is where 35.07 comes from. Anyone comparing this floor
against a `--cov-report=term` statement column will conclude, wrongly, that the
floor is miscalibrated.

The floor was confirmed to bite in both directions before being committed: at 36
the checker exits 1 naming the module (`app/activitypub/util.py: 35.07% is below
its floor of 36.00%`), and at 35 it exits 0 reporting all four module floors met.

Do not read 35 as a quality grade for this module. Every one of the six target
functions has zero missing statements and, with a single documented exception,
zero missing branch arcs -- re-derivable from any run's `coverage.json` by
intersecting each function's own AST span against the file's `executed_lines`,
`missing_lines`, `executed_branches` and `missing_branches`. The one exception is
the false side of the Feed branch's post-commit re-fetch guard, which is
unreachable (D19). No pragma was added for it; the arc is left visible and
explained in the test that names it.

### 8. Coverage cannot see inside a string -- and one defect that hid behind that

The whole-branch review that closed this sub-project raised the same failure
against three separate functions, all of which reported **100% statement and
branch coverage**. In each case the unprotected code was a string normalisation
-- `.lower()` or `.strip()` -- sitting inside an expression on a line that some
existing test already executed. Coverage records the line as covered whichever
way the call goes, so these could be deleted with the full suite green.

Thirteen call sites in total:

| function | sites | what they normalise |
|---|---|---|
| `find_community` | 4 `.lower()` | a peer's actor URI, against a lower-cased `ap_profile_id` |
| `find_flair_or_create` | 3 `.strip()` | a peer's flair name, on both lookup arms and on create |
| `actor_json_to_model` | 6 `.strip()` | `preferredUsername` and `name`, twice per branch, in all three branches |

Twelve of the thirteen were open; the thirteenth (the Person branch's `name`)
was already pinned. Each is now pinned by a test that feeds input where the
transformation decides the outcome -- a mixed-case actor URI, a padded flair
name -- rather than a test that merely executes the line again. Every mutation
was run on its own against the full suite: each site fails exactly one test, and
no site is carried by another's.

Re-derive the sites with:

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/activitypub/util.py').read()
for n in ast.walk(ast.parse(src)):
    if isinstance(n, ast.FunctionDef) and n.name in (
            'find_community', 'find_flair_or_create', 'actor_json_to_model'):
        for c in ast.walk(n):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr in ('lower', 'strip')):
                print(n.name, '|', ast.unparse(c))
" | sort | uniq -c
```

For `find_community` and `find_flair_or_create` that output *is* the table above.
For `actor_json_to_model` it is not: the table's six are
`activity_json['preferredUsername'].strip()` and `activity_json['name'].strip()`,
three of each, one pair per branch. **The same command lists ten further
normalisation sites in that function which this fix wave did not audit** --
`activity_json['id'].lower()` (10 occurrences), `server.lower()` (7),
`address.lower()` (3), `address[1:].lower()` (2), `user.title.strip()`,
`user.title.strip().lower()`, `field_data['name'].strip()` and
`field_data['value'].strip()`. Some are pinned by tests written earlier in this
sub-project (`TestApIdFromAddress` covers the address and server pair;
`test_lookup_of_an_existing_community_lowercases_the_id` and its Feed
counterpart cover the id); the rest were simply not examined. Anyone extending
this work should start there rather than assume the function is done.

**This is the generalisable lesson, and it applies to every remaining
sub-project.** A function at 100% on both metrics tells you nothing about its
string handling. If the code normalises, the suite must contain an input where
normalising is what makes the assertion true. The same blindness was already
recorded for regexes and URL literals in sub-project 1c's section 3; case and
whitespace normalisation is the third member of that family, and the easiest to
miss because the call is one word long and never on a line of its own.

**A count in the review was wrong on re-derivation**, in the usual direction.
The review recorded `actor_json_to_model`'s Person branch as pinning its
stripping properly, with three tests failing on removal, and asked only that the
Group and Feed branches be brought up to it. Mutating each of the Person
branch's two sites separately against the full suite gave one test for `name`
and **zero** for `preferredUsername` -- Person had one of its two sites open too.
All six sites in that function are now pinned, one test each. Re-derive before
carrying a figure forward; this is the eighth time in this campaign.

**D20.** Reading `find_flair_or_create`'s three `.strip()` calls surfaced a
fourth place that should have one and does not. The function resolves a
peer-supplied flair dict to a `CommunityFlair` row, and there are three paths
through it:

- the **lookup** strips: `CommunityFlair.flair == flair['preferredUsername'].strip()`
  and `... == flair['display_name'].strip()`
- the **create** path strips: `CommunityFlair(flair=flair_text.strip(), ...)`
- the **update** path does not: `existing_flair.flair = flair["display_name"]`,
  and its `elif` arm `existing_flair.flair = flair['preferredUsername']`

So a peer that sends `' spoiler '` creates the row as `'spoiler'`, and the next
delivery of the *same document* finds that row (the lookup strips, so it
matches) and overwrites its name with `' spoiler '`. The stored value flips on
the second delivery, and every delivery after the first leaves the padded form
in the column.

**Where the input comes from, and how bad that is.** The flair dict is
peer-supplied and reaches this function without whitespace validation, from
`actor_json_to_model`'s `tag` / `lemmy:tagsForPosts` handling and from
`refresh_community_profile_task`. A remote community admin controls the string.
The consequence is a display and matching defect, not a security one: the flair
renders with leading and trailing whitespace, and any lookup elsewhere that
compares an unstripped column value against a stripped input stops matching. It
does not cross a trust boundary, does not bypass a check, and cannot corrupt
another community's rows -- `community_id` scopes every query. It is also
partially self-correcting, in that the *lookup* still finds the row.

Rated **low**, and left unfixed like every other entry in this register.
`tests/test_ap_find_flair_or_create.py::TestWhitespaceInThePeersFlairName`
carries two tests that assert the unstripped value, one per arm of the update
block, with docstrings saying in as many words that they pin present behaviour
and not desired behaviour. If someone adds the missing `.strip()`, those two
tests fail -- deliberately, so the fix has to come with a considered update to
the assertions rather than sliding through green.

Two smaller asymmetries of the same shape were found alongside D20 and are
**not** filed as defects, because in both cases the unstripped column plausibly
wants the peer's raw value: `actor_json_to_model` strips `preferredUsername`
into `User.user_name` but not into `User.ap_preferred_username`, and strips it
into `Feed.name` but not into `Feed.machine_name`. Both are asserted, unstripped,
in the tests that pin the stripped column next to them, so a later change to
either cannot pass unnoticed.

## Sub-project 2b: the `netloc` reads the ingest fixes did not reach

Sub-project 2b replaced `netloc` with a real host comparison in
`ensure_domains_match`, `verify_object_from_source` and `actor_json_to_model`.
The same shape survives elsewhere in `app/activitypub/util.py`, in three
functions this campaign has never tested: `resolve_remote_post`,
`create_resolved_object` and `resolve_remote_post_from_search`. They are
registered here as D21–D24 and **not fixed** — fixing untested code is how this
campaign would start producing the defects it exists to find.

### 1. The enumeration, re-derived — and a brief that was wrong about it

The task brief said "twenty lines, one a comment", of which "seven reads are in
the two already-fixed functions". Against the checkout this section was written
from, that is wrong in three ways: there are twelve reads, not twenty; none of
them is a comment; and the already-fixed functions contain **zero** — the fixes
removed every one, which is exactly what a completed fix should look like. The
"seven" was a pre-fix figure carried forward in prose. The running count of
hand-carried numbers in this campaign found wrong on re-derivation stood at
seven before this one (see sub-project 1c), so this is the eighth — and it was
wrong in a way none of the others were: it did not merely go stale, it described
a state of the tree that the brief's own sub-project had already superseded.

The brief's *load-bearing* claim — ten reads in the three named functions — is
correct. Derived, without line numbers so the derivation stays checkable as the
file moves:

```bash
grep -c '\.netloc' app/activitypub/util.py
awk '/^def /{fn=$2; sub(/\(.*/,"",fn)} /\.netloc/{print fn}' \
  app/activitypub/util.py | sort | uniq -c | sort -rn
```

```
12

      5 resolve_remote_post_from_search
      3 create_resolved_object
      2 resolve_remote_post
      1 process_microblog_announce
      1 extract_domain_and_actor
```

5 + 3 + 2 = 10 in the three named functions. The remaining two are elsewhere:
`extract_domain_and_actor` (already hardened by this campaign against the
`urlparse` `ValueError` described below, and its `netloc` return feeds
name-and-server splitting rather than a trust comparison), and
`process_microblog_announce`.

**`process_microblog_announce`'s read belongs to this defect family even though
the brief excluded it.** It is the expression `urlparse(uri).netloc` passed as
`create_resolved_object`'s `uri_domain` argument, so it is not an eleventh
independent comparison — it is one of the *operands* of `create_resolved_object`'s
comparison, supplied from a different place than the other callers supply it.
Treating it as out of scope would have hidden a call-site difference that turns
out to matter (see section 3).

### 2. Ten reads, three comparisons

The ten reads do not make ten defects. Every one of them is an operand of
exactly one of three `!=` tests:

- **C1, in `resolve_remote_post`:** `announce_actor_domain != uri_domain`, where
  `uri_domain` is the netloc of the Announce's object URI and
  `announce_actor_domain` is the netloc of `community.ap_profile_id`. Gates
  whether an announcing community may vouch for an object URI at all.
- **C2, in `create_resolved_object`:** `uri_domain != actor_domain`, where
  `uri_domain` is a parameter and `actor_domain` is the netloc of whichever
  `attributedTo` form the fetched document carries (three reads, one per shape:
  a bare string, a `Person` dict inside a list, a string inside a list). This is
  the impersonation check the function's own comment advertises.
- **C3, in `resolve_remote_post_from_search`:** the same `uri_domain !=
  actor_domain` test, with its own inlined copies of both the `attributedTo`
  walk (three reads) and the `uri_domain` derivation (two reads — an initial one
  and a re-derivation inside the NodeBB `OrderedCollection` branch).

C2 and C3 are textual near-duplicates of each other. A fix applied to one and
not the other would leave the defect standing, which is the practical reason to
register them as separate entries rather than one.

### 3. The severity reverses again — and this time it is worse in one direction

The `netloc`-versus-host reversal that sub-project 2a established holds here.
Re-probed against this checkout, exhaustively over a sample of authorities
covering case, port, userinfo, IPv6 literals and IDNA:

```
netloc-equal-but-host-different pairs: []
```

There are none, and there cannot be: `hostname` is a pure function of the
`netloc` string, so equal netlocs always yield equal hosts. **A netloc
comparison is strictly stricter than a host comparison. It can only false-refuse;
it can never false-accept.** So the "userinfo lets a peer impersonate another
host" reading is wrong here for the same reason it was wrong in 2a, and all four
of these entries are availability defects, not boundary bypasses. This judgement
is **probe-backed**.

What is *new* here, and not present in the functions 2a fixed, is a **case
asymmetry that makes the false refusal systematic rather than conditional**. 2a's
finding was that a netloc comparison only misfires when a peer is internally
inconsistent about the port across the two fields being compared — a peer that
carries the same authority everywhere passes. That escape hatch is closed in two
of these three comparisons, because one side is lowercased and the other is not:

- **C1** compares `community.ap_profile_id`'s netloc against the object URI's.
  `Community.ap_profile_id` is stored lowercased (`actor_json_to_model` writes
  `activity_json['id'].lower()`, and every lookup filters on `.lower()`), while
  the object URI's netloc is the peer's raw string. So a peer whose community
  actor id and object URIs agree perfectly still fails C1 if its authority
  contains any uppercase character. No inconsistency on the peer's part is
  required — internal consistency is not a defence.
- **C2 reached from the API** has the same shape. `get_resolve_object` in
  `app/api/alpha/utils/misc.py` computes `server` as
  `urlparse(query).netloc.lower()` and passes it as `uri_domain`, while
  `actor_domain` is derived raw. Same systematic refusal.
- **C2 reached from `resolve_remote_post` or `process_microblog_announce`**, and
  **C3**, derive both sides raw, so they retain 2a's weaker "requires peer
  inconsistency" character.

That is a severity *difference between call sites of one function*, which is
precisely the thing a flat defect list destroys. `create_resolved_object` is
strictly worse when the API calls it than when the inbox does, and reading the
function alone cannot show that.

Two further consequences of `netloc` being carried unstripped, both in C2's
scope: `uri_domain` is interpolated into a synthesised activity id
(`f"https://{uri_domain}/activities/..."`), and the API path interpolates the
same string into `announce_id`. Userinfo or a port present in the peer's URI
therefore rides into a synthesised identifier. Because C2 has already forced
`uri_domain == actor_domain` by that point, this does not let a peer name a host
it does not control; it is a hygiene defect in a synthesised string, rated
informational and folded into D22 rather than filed separately.

### 4. Reachability — the question that governs everything else

All three functions are reachable from peer-controlled input. This was traced,
not assumed.

- **`resolve_remote_post`** is called from `process_announce_of_uri`, which
  `process_inbox_request` calls for an `Announce` addressed to a community. The
  `uri` is `announce_target_uri(request_json)` — the peer's own Announce body.
  **A peer chooses `uri_domain` outright**; `announce_actor_domain` comes from
  the local database row for the community it announced into. Peer-triggerable
  at will; blast radius is one announced post silently not created.
- **`create_resolved_object`** has three callers with three different provenances
  for `uri_domain`: `resolve_remote_post` (peer's Announce URI),
  `process_microblog_announce` (peer's Announce URI, for the microblog boost
  path), and the alpha API's `get_resolve_object` (the *local* user's search
  query URL, lowercased). `actor_domain` is peer-controlled on all three —
  it is read out of a document fetched from the remote host. So on two of three
  call sites a peer controls both operands, and on the third a local user
  controls one and a peer the other.
- **`resolve_remote_post_from_search`** the brief implied is UI-only. It is not.
  Two callers: the `retrieve_remote_post` form in `app/search/routes.py`
  (login-required, local user supplies the URI), **and the `Move` activity
  handler in `app/activitypub/routes.py`**, which passes
  `core_activity['object'].replace('/context', '')` — a peer-chosen string,
  reached whenever a peer sends a `Move` naming an origin and target community
  that both resolve. That second caller is what makes D24 a peer-triggerable
  defect rather than a user-visible annoyance, and it is worth stating plainly
  that reading the function's own comment ("called from UI, via 'search' option
  in navbar") would have led to the wrong answer. The comment is stale.

### 5. A second defect in the same reads: `urlparse` itself raises

Every one of the ten reads is preceded by a bare `urlparse(...)` with no
`try`/`except`. On the interpreter this suite runs, `urlparse` raises
`ValueError` before any attribute access, for inputs a hostile or merely broken
peer can send. Probed:

```
'https://[::1/x'          -> urlparse ValueError: Invalid IPv6 URL
'https://a<U+2100>b.example/x'  -> urlparse ValueError: netloc contains invalid
                             characters under NFKC normalization
'https://[1::2::3]/x'     -> urlparse ValueError: does not appear to be an IPv4
                             or IPv6 address
```

This is the **same defect class `extract_domain_and_actor` was hardened against
earlier in this campaign** — that function now wraps its `urlparse` and returns
`('', '')` — and the three functions here were not given the same treatment,
because nobody was looking at them. On the inbox path the exception escapes
through `process_inbox_request`'s `except Exception: session.rollback(); raise`,
so it does not reach the peer (the inbox has already returned and dispatched to
Celery); it fails the task and drops the activity with a traceback. Availability
and log-noise, not a bypass. Probe-backed for the raising behaviour;
**reading-only** for the claim about how the Celery task disposes of it.

### 6. Two adjacent findings that are *not* this defect

Both surfaced from the call-site tracing and neither is a `netloc`-versus-host
problem. They are recorded because a future fix to C1 will touch the same
expression and should not silently inherit them.

- **A hardcoded per-instance trust exemption.** C1's guard reads
  `if announce_actor_domain != 'ovo.st' and not nodebb and announce_actor_domain
  != uri_domain`. When the announcing community lives on `ovo.st`, the
  domain check is skipped entirely and that instance may announce an object URI
  on any host. This is a named-instance carve-out in a trust boundary, not a
  parsing bug.
- **The NodeBB reply fan-out skips the same guard.**
  `get_nodebb_replies_in_background` calls `resolve_remote_post` with
  `nodebb=True`, which also short-circuits C1. The URI list it iterates is
  `topic_post_data['orderedItems'][1:]`, taken verbatim from a document the
  remote host served, so a peer can list reply URIs on arbitrary third-party
  hosts and have each resolved into the community without C1 ever running. C2
  still applies to each one, so the resulting content must genuinely be served
  by, and attributed to, the host in its own URI — this is cross-host content
  *injection into a community*, not forgery. Related: inside
  `resolve_remote_post_from_search`, the `OrderedCollection` branch **re-derives
  `uri_domain` from `post_data['orderedItems'][0]`**, so a host asked for one URI
  can redirect the whole resolution to a different host and the C3 comparison
  then compares that host against itself and passes.

### 7. What the tests pin: nothing, near enough

Measured, not assumed — a full suite run with `--cov=app.activitypub.util`,
reading `executed_lines` against each function's span:

```
resolve_remote_post              executed 1   missing 11
create_resolved_object           executed 25  missing 33
resolve_remote_post_from_search  executed 1   missing 71
```

The single executed statement in each of `resolve_remote_post` and
`resolve_remote_post_from_search` is the `def` itself. **Of the ten `netloc`
reads, exactly one ever executes**: `create_resolved_object`'s bare-string
`attributedTo` branch, reached incidentally by
`tests/test_process_microblog_announce.py`'s success-path test, and only on the
matching-domain outcome. The other nine never run. `tests/test_announce_dispatch.py`
monkeypatches `resolve_remote_post` out, so it pins the call, not the body.

The consequence is the point of registering this at all: **nothing pins the
current behaviour of these comparisons.** A future fix here has no safety net —
no test would fail if the fix were wrong, and none would fail if the fix were
right either. Anyone taking D21–D24 should expect to write characterisation
tests for the present behaviour first, exactly as sub-project 2a did before its
own fixes.

### 8. Register

Severities below are for the *availability* consequence; none of D21–D24 is a
trust-boundary bypass, and that conclusion is probe-backed.

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| D21 | `resolve_remote_post` | C1 compares raw `netloc` strings, one side lowercased from the database and one side the peer's raw authority. Any peer with uppercase in its authority is refused unconditionally; port and userinfo differences refuse too. Plus an unguarded `urlparse` on a peer-supplied URI. | medium — systematic, peer-triggerable, silent | probe (comparison + `urlparse` raise); reading (Celery disposal) |
| D22 | `create_resolved_object` | C2, same shape. Systematic refusal only on the alpha-API call path, where `server` is lowercased and `actor_domain` is not; inconsistency-dependent on the two inbox paths. `uri_domain` also rides unstripped into a synthesised activity id. Carries its own unguarded `urlparse` calls on peer-supplied strings, with the same crash risk as D21. | medium on the API path, low on the inbox paths | probe (comparison); reading (call-site provenance) |
| D23 | `create_resolved_object` / `resolve_remote_post_from_search` duplication | C2 and C3 are textual near-duplicates, including both `attributedTo` walks. Fixing one leaves the other. | low, but it is the reason a fix can half-land | reading |
| D24 | `resolve_remote_post_from_search` | C3, same shape, both sides raw, so inconsistency-dependent. Reachable from peer-controlled input via the `Move` handler, which the function's own stale comment denies. Carries its own unguarded `urlparse` calls on peer-supplied strings, with the same crash risk as D21. | low–medium | reading (both call sites traced) |

Adjacent, filed but explicitly *not* the same defect: the `ovo.st` carve-out and
the `nodebb=True` bypass of C1, and the `OrderedCollection` re-derivation of
`uri_domain` — all described in section 6, all reading-only.

## Sub-project 2c: fixing fifteen of sub-project 2a's twenty defects

`docs/superpowers/plans/2026-08-28-fix-activitypub-ingest-defects.md`, on branch
`fix-ap-ingest-defects`. Ten tasks: seven that changed production code, one that
added a read-only audit command, and two documentation tasks (2b's register above
is one of them, and this section is the other). Fifteen of sub-project 2a's
twenty defects are fixed and carry their commits in that sub-project's register;
five are still open and are named at the top of it.

Sub-project 2a deliberately fixed nothing, on the ground that fixing untested
code is how a coverage campaign starts producing defects. 2c is the other half of
that bargain: every defect it fixed was already pinned by a characterisation test
written in 2a, so every fix began from a known-red state rather than from an
argument.

### 1. Five new defects found while fixing the old ones — D25–D29

None of these is fixed. Authorisation covered the fifteen defects in the plan and
none of these five is one of them, so they are registered rather than repaired —
the same rule 2a and 2b followed. Each was found by a task working on something
adjacent, and each was verified against source again when it was filed.

| # | function | defect | how it was found |
|---|---|---|---|
| D25 | `actor_json_to_model`, Person/Service | the branch stores `ap_domain=server` with the peer's authority unlowered, where the Group and Feed branches both store `server.lower()`. With `extract_domain_and_actor` also returning its authority unlowered, a legitimate `User` row can end up with an `ap_domain` that differs from `ap_profile_id`'s host in case alone — `ap_profile_id` is lowercased on the same constructor call. | the cross-host audit command (Task 4), which needs a case fold in its comparison purely because of this; without the fold it reports honest peers as smuggling suspects |
| D26 | `refresh_community_profile_task` | the unguarded `flair['display_name']` read that D13 registered in `actor_json_to_model` appears a **second** time, in this task's own legacy `lemmy:tagsForPosts` loop, with the same partially-applied-ingest shape: the task commits the refreshed community profile and only then walks the peer's flair entries, so an entry missing the key aborts the walk after the commit has already landed. No test file calls this function at all — the two that name it do so only in docstrings. | fixing D13 one caller over (Task 5) |
| D27 | `find_flair_or_create` | under `autoflush=False`, two entries in one peer-supplied list sharing a `display_name` create two `CommunityFlair` rows with the same name for the same community, because neither call can see the other's pending insert. Present-not-desired behaviour, pinned by a test rather than left to be rediscovered. | the D9 autoflush investigation (Task 5), whose two sessions behave differently for exactly this reason |
| D28 | `find_community` | the Video block's `attributedTo` walk reads `a['type']` and then `a['id']` with no guard, so a malformed element raises `KeyError`, or `TypeError` if it is neither a string nor a dict. A third crash site in this function, outside both D2 and D3 and untouched by their fixes. | fixing D2 and D3 in the same function (Task 6) |
| D29 | `actor_json_to_model`, Feed | the inbox expression ends at `activity_json['inbox']` where Person/Service and Group both end in an empty-string fallback. A Feed document carrying neither `endpoints` nor `inbox` is therefore refused where the other two branches accept it and store `''`. D12's fix gave Group the fallback; the Feed branch was outside that defect's scope, so what used to be a two-way asymmetry is now a three-way one. | adding the Feed branch's `except KeyError` (Task 7), which turned this from a crash into a silent refusal and so made it visible |

D25, D26, D28 and D29 are all reachable from a peer-supplied document. D27 needs
only a peer sending two flair entries with the same name.

### 2. The floor after the fixes: it rose, but not by a whole point

Measured on the full suite, green at **2566 passed, 3 skipped, 0 failed**:

```bash
./run_tests.sh tests/ -q --cov=app --cov-report=json
podman-compose -f compose.test.yaml exec -T -w /app test-runner \
  python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

`percent_covered` for `app/activitypub/util.py` came back **35.653153153153156**,
against 35.06818181818182 when sub-project 2a set the floor. The checker reports
all four module floors met. Rounded down that is **35** — which is the floor
already in `coverage_floors.ini`, so the floor **holds and is not raised**. The
file is unchanged, and that is the honest outcome rather than a missed step: a
ratchet only moves when a whole point has been earned, and 0.58 of a point has.

As in section 7 of sub-project 2a, `percent_covered` is the **blended
statement+branch figure** — `.coveragerc` sets `branch = True`, so it is blended
whether or not `--cov-branch` appears on the command line, and this run is
1583 of 4440: 1068 of 2846 statements plus 515 of 1594 branches. Anyone comparing
35.65 against a statement column will conclude, wrongly, that the floor is
miscalibrated.

**The direction is the thing worth checking, and it is the right one.** These
fixes added guarded branches, and a guard whose new arm no test exercises pushes
the blended figure *down*. The figure went up, so no guard went in untested.

The floor was exercised anyway, because a floor nobody has seen fail is not a
floor: set to 36 against this run's `coverage.json` the checker exits 1 with
`app/activitypub/util.py: 35.65% is below its floor of 36.00%`, and back at 35 it
exits 0 with `All 4 module floors met.` The file was returned to its committed
contents; all four floors, including the two 100s and `app/utils.py`'s 73, are
untouched.

### 3. Every test failure this sub-project did not predict

Two, across ten tasks — plus one notable failure that was predicted and did not
arrive, which turned out to matter more than either.

1. **Task 7, and it found D29.** The brief listed the tests that adding
   `except KeyError` to the Group and Feed branches would flip. The list was one
   short: the Feed file's `test_neither_endpoints_nor_inbox_raises_key_error`
   flipped too. It meant the Feed inbox read happens *inside* the `Feed(...)`
   constructor call, so it sits inside the new `try` and the new handler catches
   it. The implementer investigated the extra flip instead of adjusting the
   expected count, which is what turned a miscounted brief into D29 above.
2. **Task 8, an exception type rather than a test name.** The brief said D15
   "raises `IndexError`". Two of its three crashes do; the case where the
   resolver rejects the first owner raises `AttributeError` instead
   (`'NoneType' object has no attribute 'id'`). A pinning test written to the
   brief's `pytest.raises(IndexError)` would have failed on that case. It meant
   the brief named one of two failure modes; both needed guarding, and both were
   guarded.
3. **The inverse, in Task 2, and it was the more informative of the three.**
   Changing `verify_object_from_source` to return a tuple was predicted to fail
   all 28 tests in its file. It failed 25. The three that passed did so because
   `is not None` is true of a tuple — their assertions had stopped meaning
   anything the moment the return type changed, and would have gone on passing
   forever. All three were strengthened. A test that fails to fail is the same
   defect as this campaign's dominant failure mode, seen from the other side, and
   a signature change is an unusually good detector for it.

Everything else this sub-project's briefs got wrong — three stale enumerations, a
mutation table that had been reasoned rather than run, a `netloc` count that was
correct when derived and stale when used, two mutation directions that turned out
to be one — was found by re-deriving a claim, not by a red test. That is the
campaign's standing lesson restated: the suite does not report a false
explanation, so nothing but re-derivation will.

### 4. Three test gaps found but not filled — since filled, in `6f1ef32a`

Each was found by a reviewer, is cheap, and was not held by anything in the
suite. They were carried to the whole-branch review, and written down here so
they would survive it not picking them up. It did pick them up: all three are
closed by `6f1ef32a`, and the mutation evidence for each is in that commit's
message and in the test docstrings. The descriptions are kept as written, since
they are what the tests were built to satisfy.

1. **Two handler-breadth pinning tests, one per Feed handler** (about twenty
   lines, no new fixtures). Broadening the Feed constructor's `except KeyError`
   to `except Exception` failed three tests before D15 was fixed and fails none
   after — fixing D15 removed the only thing policing that handler's breadth.
   The `/following` handler's equivalent broadening was never policed at any
   point. Both handlers are correct as written; what is gone is the evidence.
   Each test should send a document that raises a non-`KeyError` inside the
   `try` and assert the exception propagates with `Feed.count() == 0`.
2. **A test for the `moderators: null` path** — three lines in `TestOwnersUrl`.
   D17's fix carries a comment claiming to cover it and nothing pins it.
3. **`find_community` given a bad addressing element followed by a good one.**
   Written during Task 6's review, passing, never committed. It is the
   difference between "skips the bad entry" and "stops at the bad entry", and
   only one of those is what the D3 fix claims.

## Sub-project 2c's whole-branch review: four more defects — D30–D33

The review that closed `fix-ap-ingest-defects` read the branch as a whole rather
than task by task, and found four more. **None is fixed.** Same rule as D25–D29:
authorisation covered the fifteen defects in the plan and none of these four is
one of them, so they are registered rather than repaired. D30–D33 are taken here,
in the commit that writes these rows.

Every claim below was re-verified against source when the row was written, and
**two of the four descriptions handed over by the review were wrong in detail** —
right about the defect, wrong about the surrounding code. Both corrections are
recorded under their rows rather than quietly applied, because the pattern of
"right in substance, wrong in detail" is this campaign's standing hazard and the
count of how often it happens is itself evidence.

| # | function | defect | how it was found |
|---|---|---|---|
| D30 | `actor_json_to_model`, Group | the new-style flair loop reads `flair["type"]` on every element of the peer's `tag` list with no guard of any kind, and the loop runs **after** the Community has been committed. A dict without a `type` key raises `KeyError`; anything that is not a dict raises `TypeError`. Both abort the walk with the Community row already in the database and the remaining flair entries unprocessed — **partially-applied ingest**, the same shape as D13 one loop away. D13's fix guarded the legacy `lemmy:tagsForPosts` loop in the `elif`; this is the `if` arm above it and was outside that defect's scope. | reading the Group branch end to end for post-commit surfaces, during the whole-branch review |
| D31 | `actor_json_to_model`, Feed | `for child_feed in activity_json['childFeeds']:` is guarded only by `'childFeeds' in activity_json`, so `childFeeds: null` raises `TypeError: 'NoneType' object is not iterable`. The loop runs after **three** commits — the Feed, then a commit per FeedMember, then a commit per FeedItem — so the feed, its owners and its followed communities are all already written. **Partially-applied ingest**, and the deepest instance of the shape in the file by number of preceding commits. A non-null scalar (`childFeeds: 5`) raises the same way; a *string* value does not raise at all, it iterates the string's characters and hands each one to `populate_child_feed`, which is a separate and quieter wrong. | the same read, one branch over |
| D32 | `actor_json_to_model` | the host gate `host_of(activity_json['id']) != host_of(f'//{server}')` is sound only while `server` is non-empty: `host_of` degrades an unparseable string to `''`, and `'' != ''` is False, so two failed parses pass the gate. Four of the five call sites keep `server` non-empty; the fifth, `create_actor_from_remote` in `app/activitypub/actor.py`, does not — on its `https://`/`http://` path it takes `server` from `extract_domain_and_actor`, which returns `('', '')` on a `urlparse` `ValueError`, and then fetches with `actor_address`, a different variable, so nothing ever exercises `server`. **Reachability is UNPROVEN and is not claimed:** it needs httpx to accept and successfully fetch a URL that Python's `urlparse` refuses. Nobody has exhibited such a URL. This is filed as a latent gap in an assumption `host_of`'s own docstring records, not as a demonstrated hole. | tracing `host_of`'s stated caller obligation to each call site while correcting its docstring |
| D33 | `actor_json_to_model`, Group | the branch's `except KeyError` wraps a `Community(...)` call whose keyword arguments include `instance_id=find_instance_id(server)` — which **commits an `Instance` row** when the peer is new — followed, later in the same argument list, by `content_retention=current_app.config['DEFAULT_CONTENT_RETENTION']`. Keyword arguments evaluate in source order, so a missing config key raises `KeyError` after the Instance commit has landed, and the handler swallows it and returns `None`. The caller sees "malformed peer document"; the truth is a misconfigured deployment, and a sparse Instance row plus a `new_instance_profile` fetch are left behind. Deployment error rather than peer input, which is why it is filed separately from D30/D31 rather than as another instance of the shape. | the same read; `find_instance_id` is easy to miss as a writing call because it is spelled as a lookup |

**Correction to D32 as it was handed over.** The review said the four safe call
sites "guarantee `server` non-empty by building their fetch URL from it". They do
not all do that, and the distinction matters to anyone auditing a new call site:

- `search_for_user` in `app/user/utils.py` and `get_resolve_object` in
  `app/api/alpha/utils/misc.py` carry an **explicit `if not server:` refusal**
  before they fetch anything.
- `search_for_community` in `app/community/util.py` and `search_for_feed` in
  `app/feed/util.py` carry no such guard and rely entirely on the webfinger URL
  being built as `f"https://{server}/.well-known/webfinger"`, which cannot answer
  200 with an empty `server`.
- The fifth site has **two** paths and only one is unguarded. Its webfinger path
  reaches `fetch_actor_from_webfinger(address, server)`, which does build its URL
  from `server`; only the `https://`/`http://` path is exposed.

So the discharge is 2 explicit + 2 incidental + 1 path-dependent, not "four the
same way". An explicit guard survives a refactor of the fetch; an incidental one
does not.

**D30 and D31 are now fixed**, on branch `fix-ingest-shape`, under separate
owner authorisation. D32 and D33 remain registered and unfixed. Both fixes are
the guard-and-skip that D13 established: the malformed optional value is tested
for, skipped, and **logged**, matching how the code beside it already treats
every other optional key. D30's `tag` loop gained `if not isinstance(flair,
dict) or "type" not in flair: continue`; D31's `childFeeds` value must now be a
list, and anything else is ignored with a warning naming the type it got.

**Two corrections to D31 as this table states it.** Both were found by fixing
it, and both change what a fix has to be tested against:

- **"the deepest instance of the shape in the file by number of preceding
  commits" is not established.** It holds only among the six *registered*
  instances. `refresh_community_profile_task`'s featured-collection walk —
  unregistered at the time of writing — sits behind more, and the read-only
  sweep in `.superpowers/sdd/fix-ingest-shape-sweep.md` records eleven further
  candidates besides. The claim still stands verbatim in the row above, per this
  table's convention that corrections are recorded under their rows rather than
  quietly applied — so read the row and this note together. Treat the
  superlative as withdrawn: it is not worth re-scoping, because a superlative
  over a set that is still growing will be wrong again by the next sweep.
- **The row names the string case but omits the MAPPING case, and omits that
  the string case can succeed.** `childFeeds: {...}` does not raise either: it
  iterates the object's keys, so a child feed url sent as a key is linked
  exactly as though the peer had sent a list. That is the quietest of the three
  — it looks like it worked. And the string case is not only "quieter", it can
  *complete*: each single character resolves through `search_for_feed('~a@')`,
  and any feed whose `ap_id` is `a@` is reparented onto the feed being
  ingested. Under `.delay()` that is N celery tasks in production and an ingest
  that returns normally; the failure surfaces inline only under DEBUG. Neither
  silent case would have been caught by a test written from the `null` case
  alone, which is why the fix's three tests are one flipped and two
  characterisation.

**One thing NOT fixed, and reported rather than repaired** because it is outside
D30/D31: a `childFeeds` value that *is* a list, whose elements are not strings,
still reaches `extract_domain_and_actor` inside `populate_child_feed_worker`.
Same shape, one level down.

**Correction to D33 as it was handed over.** The review said "Feed and Person
order these safely". They do not order anything — **neither try contains a
`current_app.config` read at all**, so there is nothing to order. The Group
branch is the only one of the three with a config read inside a `try`. Derived,
not read:

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/activitypub/util.py').read()
func = next(n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == 'actor_json_to_model')
for t in ast.walk(func):
    if isinstance(t, ast.Try):
        names = [ast.unparse(h.type) if h.type else 'bare' for h in t.handlers]
        cfg = [ast.unparse(c) for c in ast.walk(t) if isinstance(c, ast.Subscript)
               and 'current_app.config' in ast.unparse(c)]
        fii = [ast.unparse(c) for c in ast.walk(t) if isinstance(c, ast.Call)
               and ast.unparse(c.func) == 'find_instance_id']
        print('handlers=', names, '| config:', cfg, '| find_instance_id:', fii)
"
```

That prints seven `Try` nodes. Three carry `except KeyError` and call
`find_instance_id`; exactly **one** of those three also carries a
`current_app.config` read, and it is the Group one. The other four handle
`IntegrityError` and carry neither.

### The partially-applied-ingest shape, counted honestly: six, not five

The review's handover said the shape "has now been found five times in this
file", and then listed six. Six is the number. It is worth stating exactly
because the shape is the single most productive reading heuristic this campaign
has produced, and undercounting it makes it look like a closed set.

In `app/activitypub/util.py`:

- **Fixed:** D9 (`find_flair_or_create`'s ap_id backfill, via
  `refresh_community_profile_task`), D13 (`actor_json_to_model`'s Group branch,
  legacy `lemmy:tagsForPosts` loop), D16 (`actor_json_to_model`'s Feed branch,
  the /following collection's rejected entries), and now D30 (the Group
  branch's new-style `tag` loop) and D31 (the Feed branch's `childFeeds` loop).
- **Also fixed, after this section was first written:** D26
  (`refresh_community_profile_task`'s own legacy flair loop), which brought that
  task its first tests, since nothing had ever called it.
- **Registered and unfixed:** none of the six. Eleven further candidates for the
  same shape are recorded in the read-only sweep at
  `.superpowers/sdd/fix-ingest-shape-sweep.md`, none of them registered here yet
  and none in tested code.

Six of six, as of the `fix-ingest-shape` work; three plus three when this
section was written. Derivable from this document rather than counted by hand:

```bash
grep -ciE '^\| \*{0,2}D[0-9]+.*partially-applied.ingest' \
  docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
```

That prints **6**, and `-i` is load-bearing rather than habit: one of the six
rows opens the sentence with the phrase and so capitalises it. Dropping `-i`
prints 5, which is exactly the undercount this section exists to correct.

**And `find_flair_or_create` contains no commits at all.** This is the part of
D9 most easily lost, and it explains why the shape is a property of a *pair* of
functions rather than of one:

```bash
podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
import ast
src = open('app/activitypub/util.py').read()
func = next(n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == 'find_flair_or_create')
print([ast.unparse(c) for c in ast.walk(func)
       if isinstance(c, ast.Call) and ast.unparse(c.func).endswith('.commit')])
"
```

That prints `[]`. The partial-ingest risk at that function was never its own —
it belonged to whichever caller had already committed before calling it. Which
is precisely why D9 fires from `refresh_community_profile_task`, whose session
comes from `get_task_session()` and commits the refreshed profile first, and
*not* from `actor_json_to_model`, whose `db.session` is constructed with
`autoflush=False` so the same-call collision never forms. The defect is in the
composition, not in either function alone. **Read every ingestion function
together with its callers, or this shape is invisible.**

## Sub-project 3: the resolver functions, and what the tests say about D21-D24

`docs/superpowers/plans/2026-08-28-coverage-resolve-functions.md`, on branch
`coverage-resolve-functions`. Six test tasks over `resolve_remote_post`,
`create_resolved_object` and `resolve_remote_post_from_search` — the three
functions sub-project 2b registered and deliberately left untested. 92 tests,
and the two large functions are now at 55/60 and 73/73 statements.

Sub-project 2b's closing sentence was that anyone taking D21-D24 should expect
to write characterisation tests first, because nothing pinned the current
behaviour. That is what this is. **Nothing here is fixed**; the authorisation
covered testing and reporting only.

### 1. The five registered defects, verified against tests rather than reading

| # | verdict | the test that demonstrates it |
|---|---|---|
| D21 | **confirmed** | `TestRawNetlocComparison` in `tests/test_ap_resolve_remote_post.py` — a community host differing from the object URI's only in case, and one carrying an explicit `:443`, are both refused with no post written |
| D22 | **confirmed** | `TestRawNetlocComparison` in `tests/test_ap_create_resolved_object.py` — case, explicit port and userinfo all refuse |
| D23 | **confirmed, and understated** — see section 2 | the AST equality check and the drift table in `tests/test_ap_resolve_from_search.py` |
| D24 | **confirmed** | `TestThisCopysDomainGate` in `tests/test_ap_resolve_from_search.py` |
| `posted_at` | **confirmed in full** — see section 3 | `TestAPublishedValueTheColumnCannotStore` in `tests/test_ap_create_resolved_object.py` |

**One qualification on D21, because the register's wording is stronger than what
a test can show.** The row says the refusal is *systematic* — that
`community.ap_profile_id` is stored lowercased while the object URI's netloc is
the peer's raw string, so internal consistency on the peer's part is no defence.
The comparison's case-sensitivity is now pinned by test. The premise about how
`ap_profile_id` comes to be lowercased is still **reading-only**: the test
constructs the community row directly rather than ingesting an actor document,
so it demonstrates the refusal without demonstrating that production always
supplies a lowercased left operand. Anyone fixing D21 should keep that
distinction in view — the fix is the same either way, but the severity rests on
the unproven half.

D22's systematic claim, by contrast, **is** pinned: `uri_domain` is a parameter,
the alpha API passes it lowercased, and the test passes it the same way.

### 2. D23 is a triple, not a pair, and the duplication is exact

Two corrections to the row, both mechanical:

- **The two unfixed copies are byte-identical**, not "textual near-duplicates".
  Normalised through the AST, `create_resolved_object`'s walk and
  `resolve_remote_post_from_search`'s walk compare equal. Every finding recorded
  against one holds verbatim against the other.
- **There is a third copy, and it is already fixed.** `verify_object_from_source`
  carries the same walk and the same gate, and sub-project 2b fixed it to use
  `host_of`. So the family is three functions wide, one member is correct, and
  D23's "fixing one leaves the other" is not a forecast — it has already
  happened, in this file, in this campaign.

The fixed copy is therefore the **fix template** for the other two, and the
differences are enumerated rather than left to be rediscovered:

| difference | the two unfixed copies | `verify_object_from_source` | behaviour? |
|---|---|---|---|
| host comparison | `urlparse(...).netloc` | `host_of(...)` | yes — this is D22/D24 |
| a bare embedded object as `attributedTo` | no arm matches; falls through with `actor_domain` None | `elif isinstance(..., dict) and 'id' in ...` | yes — see D38 |
| an unusable `attributedTo` type | silent fall-through, then refused by the domain gate | `else: return None, '<reason>'` | yes — diagnosis |
| arm order | `Person`-dict arm first | string arm first | no — one element matches at most one arm |

The arm-order row is stated so that whoever deduplicates these knows it is safe
to normalise; the other three are the work.

**And a fifth difference, in a neighbouring expression rather than the walk:**
the two copies disagree with each other about `inReplyTo`.
`create_resolved_object` tests it for truthiness, `resolve_remote_post_from_search`
tests `is not None`. A present-but-empty-string `inReplyTo` therefore becomes a
Post through one and a reply attempt through the other. Both behaviours are now
pinned, one in each file, and the mutant that makes them agree fails a test.

### 3. The `posted_at` defect: confirmed, and the register was right about all of it

Observed, not inferred: a `published` value the column cannot store raises
`sqlalchemy.exc.DataError` (wrapping psycopg2's `InvalidDatetimeFormat`) out of
`create_resolved_object`, and the Post row survives, because `create_post`
committed it before the enrichment ran.

**A correction to this sub-project's own reporting.** Task 3's commit message
presented the surviving row as something the register had missed. It had not:
the design document for this sub-project states the defect as "fails at flush
with the post already committed". Both halves were on the record; this work
observed them. The claim of novelty was wrong and is withdrawn here rather than
left in the commit log unqualified — the campaign's rule about re-deriving
claims applies to its own reports too.

What is genuinely new is the sibling defect below (D37): a `published` value the
column CAN store is silently wrong whenever it carries a non-zero offset.

**And a note for the partially-applied-ingest count**, which an earlier section
of this document makes derivable by grep. This defect is a seventh instance of
that shape — the row is committed, the enrichment then fails, the exception
escapes — but it is registered in this sub-project's design document rather than
as a D-numbered row here, so:

```bash
grep -ciE '^\| \*{0,2}D[0-9]+.*partially-applied.ingest' \
  docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
```

still prints **6**, and that is correct for what it measures: D-rows carrying
the phrase. The count of known instances of the shape is seven. The two numbers
differ for a reason, and neither is wrong — but a future reader comparing the
grep against prose elsewhere needs this sentence to reconcile them, which is the
same hazard the "six, not five" section was written to fix.

### 4. Severities, re-read now that the behaviour is pinned

The severities stand as written. Nothing found here converts any of D21-D24
into a trust-boundary bypass, and 2b's probe-backed reasoning — that a netloc
comparison is strictly stricter than a host comparison, so it can only
false-refuse — held up against every case written. All four remain availability
defects.

One adjustment of emphasis rather than severity: D23's low rating was justified
by "it is the reason a fix can half-land". With the duplication now known to be
exact and three-way, and one copy already diverged by a fix, that reason is
stronger than the rating suggests. The row is left at low because nothing is
wrong in either copy on its own.

### 5. What a fix must preserve, per defect, now that tests exist

- **D21.** The `ovo.st` carve-out and the `nodebb=True` bypass are separately
  covered and are *not* part of this defect; a host-comparison fix must leave
  both firing exactly as they do. `TestOvoSt` and `TestNodebbBypass` fail if
  either is disturbed. The two `TestRawNetlocComparison` cases flip from refusal
  to a created post when the fix lands — they are written to be flipped, and
  the docstring says so.
- **D22.** Same, plus: the gate must keep refusing when `actor_domain` is None,
  which is a different path from a domain mismatch and has its own tests. A fix
  that normalises hosts must not accidentally make `None` compare equal to
  anything.
- **D23.** Any deduplication must preserve the `inReplyTo` divergence
  deliberately or change it deliberately — the two copies genuinely disagree,
  and a merge that picks one silently changes behaviour on the other path. The
  mutants for both spellings are recorded in the two test files.
- **D24.** The `Move` handler is a real caller; the function's own comment
  denies it. A fix should correct the comment, since the stale comment is what
  made this look like a UI-only concern.
- **`posted_at`.** A fix that parses `published` must keep the enrichment
  skipped when the key is absent, and must decide explicitly what to do with an
  offset — see D37, which the current code answers by silently discarding it.

### 6. New defects found while testing — D34-D40

None fixed, same rule as every sub-project before this one. **Next free number
after these: D41.**

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| **D34** | `create_resolved_object` | the third operand of `if user and community and post_data` cannot be the deciding one. Every falsy `post_data` leaves `actor` None, and `find_actor_or_create(None)` raises `AttributeError` on `actor.strip()` before the conjunction is evaluated. Dead as a decision. | informational | test (`TestPostDataOperandIsDead`) + surviving mutant |
| **D35** | `create_resolved_object` **and** `resolve_remote_post_from_search` | **no remote reply can be created by either resolver.** Both synthesise their activity as `{'id': ..., 'object': post_data}` with no `'type'` key; `Post.new` reads that key defensively, `PostReply.new` reads `request_json['type']` unguarded, so it raises `KeyError('type')`, `create_post_reply` swallows it, and the resolver returns None. Every reply arriving by Announce, by microblog boost, or by the alpha API is silently dropped. | **medium-high — silent, total for replies, three call paths** | test in both files, plus the APLOG message pinned as `'type'` |
| **D36** | both resolvers | the reply branch's enrichment and, in `resolve_remote_post_from_search`, the `return object.post` contract are unreachable while D35 stands. Not defects themselves; recorded so that fixing D35 is known to also un-dead three code paths that no test can currently exercise. | informational | branch-arc measurement |
| **D37** | both resolvers | `posted_at` is `timestamp without time zone` and the peer's raw string is assigned to it, so Postgres **discards a non-zero offset rather than converting it**. A peer publishing at `00:00+05:00` is recorded as `00:00` — five hours late — and `last_active` with it, which is what orders community listings. Stores cleanly; simply wrong. | low-medium — silent, affects ordering | test (`TestAPublishedOffsetIsDiscardedNotConverted`) |
| **D38** | `create_resolved_object`, `resolve_remote_post_from_search` | an `attributedTo` that is a single embedded object — `{'type': 'Person', 'id': ...}`, ordinary ActivityStreams — matches neither the string arm nor the list arm, so the author is never found and the document is refused. The same object inside a one-element list is accepted. **`verify_object_from_source` already handles it**, so the fix exists in-file. | low-medium — peer-triggerable availability | test in both files; the fixed copy's dict arm |
| **D39** | `resolve_remote_post_from_search` | `post_data['id']` in the second existence check is an unguarded read on a peer document, sitting between two guards that use the `'key' in ...` idiom correctly. A document without `'id'` raises `KeyError` out of the function; on the `Move` path a peer chooses that document. | low — availability | test (`test_a_document_with_no_id_raises_keyerror`) |
| **D40** | `resolve_remote_post_from_search` | the `and nodebb` conjunct in the `find_community` fallback does no work. `topic_post_data` diverges from `post_data` only in the OrderedCollection branch, which is the only place `nodebb` becomes True; with `nodebb` False the fallback would repeat a lookup that has already returned None. | informational — code quality | surviving mutant, with the divergence argument |

D35 is the one worth acting on soonest. It is not a parsing subtlety: it is an
entire class of federated content that neither resolver can ingest, failing
silently, with the traceback swallowed by a bare `except Exception` and no log
line unless `LOG_ACTIVITYPUB_TO_DB` happens to be on.

### 7. What the mutation runs cost, and what they caught that reading did not

52 mutants across the six tasks. Five survive and are reported rather than
chased — D34 and D40 above, the reply-branch fallback and the return shape that
D36 covers, and `activity` forced to `'update'`, which the update path's
fallback to create makes unobservable.

**Four tests that pinned nothing were caught by mutants and rewritten**, and
they had one shape between them: asserting a row's identity or a `None` rather
than what the path wrote.

1. `create_resolved_object`'s `user` operand — deleting it left all 16 tests
   passing, because `create_post` swallows the resulting exception and returns
   the same None. Now asserts the operand is never reached, via the APLOG entry
   the swallowed exception would write.
2. The post-update dispatch — asserted the returned id and the row count, and
   passed with the dispatch forced to `'create'`, because `Post.new` returns the
   **existing row** on a duplicate `ap_id`. Now asserts the body was rewritten.
3. The `uri_domain` reassignment — asserted through the second existence check,
   which answers before the domain gate reads `uri_domain`. Now creates the post
   rather than finding one.
4. Four NodeBB guard tests stored their row under the request URI, so the
   **entry** check answered and no fetch happened at all. Caught by respx's
   `assert_all_called` reporting four registered, never-called routes — not by
   a mutant, and not by anything a reader would have noticed.

That last one is the useful generalisation: `assert_all_called` is a coverage
check on the *fixture*, and it caught a vacuity that neither coverage nor
mutation would have. A test that never reaches the code under test still passes
its own assertions.

## Sub-project 4: the inbox gate — `shared_inbox`, its route aliases, and `replay_inbox_request`

`docs/superpowers/plans/2026-08-28-coverage-inbox-gate.md`, on branch
`coverage-inbox-gate`. Seven tasks covered `app/activitypub/routes.py`'s
inbox gate: `shared_inbox` itself (bound to `POST /inbox`), the three bare
`return shared_inbox()` route aliases that dispatch to it (`site_inbox` at
`/site_inbox`, `user_inbox` at `/u/<actor>/inbox`, `community_inbox` at
`/c/<actor>/inbox`), and the separate `replay_inbox_request` function that
shares most of `shared_inbox`'s shape but neither its route decorator nor its
protections. 46 tests across three files:
`tests/test_inbox_gate_refusals.py` (20), `tests/test_inbox_gate_signatures.py`
(7), `tests/test_inbox_gate_dispatch.py` (19) — 45 from the seven tasks, plus
the D47 characterisation test the whole-branch review added.

### 1. Whole-function coverage, and every gap explained

Measured against this sub-project's own suite
(`./run_tests.sh tests/test_inbox_gate_refusals.py tests/test_inbox_gate_signatures.py tests/test_inbox_gate_dispatch.py -q --cov=app.activitypub.routes --cov-report=json`,
45 passed at the time of measurement; 46 now, the extra test being the D47
characterisation test, which exercises gap 2's arc below), by intersecting each
function's AST span against `coverage.json`'s
`executed_lines`/`missing_lines`/`executed_branches`/`missing_branches` — the
same method sub-project 2a used:

| function | span | statements | branches |
|---|---|---|---|
| `shared_inbox` | 625-763 | 91/94 executed | 40/42 arcs executed |
| `replay_inbox_request` | 781-835 | 38/38 executed | 21/22 arcs executed |

Four gaps in total, and none is left unexplained:

1. **`shared_inbox` lines 632-634, the `except BlockingIOError:` arm.**
   Dead code — see D41 below. This is the only missing-*statement* gap either
   function has; every other gap is a missing branch arc on a line that does
   execute.
2. **`shared_inbox` branch 674→677, the False arm of
   `if not instance_allowed(furl(request_json['actor']).host):`.** Under
   `ALLOWLIST_STRONG`, this sub-project's one allowlist test
   (`test_a_disallowed_actor_is_refused_under_strong_allowlist`,
   `tests/test_inbox_gate_refusals.py`) relies on the `AllowedInstances` table
   being empty by construction — every test truncates it — so at the time this
   section was first written only the refusal (674→675, `return '', 403`) was
   exercised. **The original wording here — "Explained, not fixed: closing it
   needs one more fixture, not a defect" — was wrong and has been removed.**
   It assumed the only way to reach the continuation arm is a peer whose host
   is genuinely in `AllowedInstances`. It is not. A peer on a
   NON-allowlisted host reaches the same arm by sending `actor` as a JSON
   object instead of a string, because `furl(<dict>).host` is `None` and
   `instance_allowed(None)` returns `True` unconditionally — a live bypass of
   the allowlist control, now filed as **D47** below and pinned as
   characterisation by
   `test_a_dict_shaped_actor_skips_the_strong_allowlist_check`
   (`tests/test_inbox_gate_refusals.py`), which exercises this arc. The
   remaining untested scenario is narrower than the original text claimed:
   the ordinary allowlisted-peer path, which would indeed need one
   `AllowedInstances` fixture, and which no test in this sub-project builds.
3. **`shared_inbox` branch 741→747, the False arm of `if actor.instance_id:`.**
   Every dispatch-path test in `tests/test_inbox_gate_dispatch.py` builds its
   actor through `signing_peer` or an equivalent factory that attaches a real
   `Instance` row, so `actor.instance_id` is always truthy by the time this
   line runs. `User.instance_id` (`app/models.py:558`) is a nullable foreign
   key, so a `User` row with no instance is possible in principle — but
   nothing in this sub-project constructs one, and whether `find_actor_or_create_cached`
   or `actor_json_to_model` can ever produce a remote actor with a null
   `instance_id` was not investigated here. Left as an unexercised branch
   rather than a defect, because no test reaches it, not because it is
   provably dead.
4. **`replay_inbox_request` branch 796→802, the False arm of the
   already-present-local-content check.** The two Announce tests in
   `tests/test_inbox_gate_dispatch.py` cover the missing-fields path (line 789
   True) and the already-present-local-content path (line 796 True); no test
   sends a well-formed Announce of a genuinely remote, non-local object, which
   is the ordinary case this branch exists to let through to the PeerTube
   check and beyond. A real gap in this sub-project's own scenario coverage,
   not a defect in the function — the branch's both arms are behaviourally
   sound as read; only the pass-through arm went untested.

### 2. New defects — D41-D48

Each was found by an implementer and independently confirmed by a reviewer
against source during Tasks 2-7; each is re-verified against source again
here, at the point the row is written, per this document's standing rule.

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| D41 | `shared_inbox` | the `except BlockingIOError:` arm (routes.py:632-634) is unreachable. `request.get_json(force=True)` reads through Werkzeug's `LimitedStream`; `LimitedStream.readinto()` catches `(OSError, ValueError)` — `BlockingIOError` is an `OSError` subclass — and calls `on_disconnect()`, whose default behaviour raises `ClientDisconnected`, which subclasses `BadRequest`. The sibling `except werkzeug.exceptions.BadRequest as e:` immediately above (line 629) therefore catches every case the `BlockingIOError` handler was written for, first. Confirmed against installed Werkzeug 3.1.8's actual source (`wsgi.py`'s `LimitedStream.readinto`, `exceptions.py`'s `ClientDisconnected(BadRequest)`) a third time while writing this row, after the implementer and the reviewer each verified it independently during Task 2. | informational — dead code, no behaviour to trigger | reading + source inspection, verified three times |
| D42 | `object_has_missing_fields` (`app/activitypub/util.py:4659-4663`), reached from `shared_inbox` | returns `False` for any object typed `OrderedCollection` without checking `id`/`actor`/`object` at all. A peer-supplied Announce whose inner object is `{'type': 'OrderedCollection'}` — no `id`, no `actor`, no `object` — therefore passes the missing-fields check at `routes.py:657`, and the very next line that assumes it passed, `id = object['id']` at `routes.py:671`, raises an unhandled `KeyError` rather than producing one of the gate's normal logged 200-refusals. Confirmed against source again while writing this row: `object_has_missing_fields`'s `OrderedCollection` short-circuit is exactly as described, and `routes.py:671` sits inside the same `if request_json['type'] == 'Announce' and isinstance(...)` block that already ran `object_has_missing_fields`, with no exception handling between them. `replay_inbox_request` does not carry the equivalent `id = object['id']` reassignment, so this specific crash is `shared_inbox`-only, not shared by both entry points. | medium — peer-triggerable, unhandled exception instead of a graceful refusal | reading + source inspection |
| D43 | `shared_inbox` | the PeerTube branch (`routes.py:685`, `return ''`) has no status code, where its immediate neighbours among the early-refusal returns (e.g. `routes.py:663`, `:669`, `:679`, all `return '', 200`) specify one. Flask defaults an unspecified return to 200, so the two are indistinguishable to any caller — this is a readability/consistency gap, not a behavioural one. | cosmetic | reading |
| D44 | `replay_inbox_request` | diverges structurally from `shared_inbox` in five ways, verified against `routes.py:781-835`: no signature checks of any kind (no precheck, no `verify_request`, no LD fallback); no redis duplicate suppression; an ACTIVE `is_local()` refusal at 824-826 where `shared_inbox`'s equivalent at 710-712 is commented out; no instance bookkeeping (`last_seen`/`dormant`/`gone_forever`/`failures`/`ip_address` are never touched); and both dispatches (`process_delete_request`, `process_inbox_request`) are direct and unconditional, with `store_ap_json` hardcoded `True` and no `current_app.debug` split. **This row is scoped to the structural fact of the divergence and to the claim "no unauthenticated peer can reach this function directly"** — both true and, on their own, low-consequence. **The consequence of the divergence when it IS reached is a separate, higher-severity finding: see D45.** | low, as a standalone reachability claim — see section 3 and D45 for the reachable, higher-severity half | reading (five divergences) + reading (all three call sites traced) |
| D45 | `replay_inbox_request`, reached via `activity_replay` (`app/admin/routes.py:1270`) | **admin-mediated replay of peer-authored content bypasses every signature check `shared_inbox` performs.** `activity_replay` is gated by `@login_required` + `@permission_required('change instance settings')`, and it re-feeds a stored `ActivityPubLog.activity_json` row — content a peer sent, not content the admin authored — straight into `replay_inbox_request`, which (per D44) runs no precheck, no `HttpSignature.verify_request`, and no LD-signature fallback. An admin who replays a row that was originally logged as a signature FAILURE, or any row at all, gets it processed as if it had just arrived and passed verification. This is the brief's named case for why a flat "not peer-reachable" rating understates D44: the trigger requires a privileged action, but the content being trusted is entirely peer-controlled and the bypass, once triggered, is total rather than partial. Comparable in kind, not in trigger, to D21/D24/D42's "medium — peer-triggerable" rating for effects reached only through a traced indirect path; rated at the same tier here because the traced path is real and the effect (full signature bypass on peer content) is more severe than any of those three, offset by needing a deliberate privileged action rather than being reachable at will. | medium — admin-triggered, but a complete signature-verification bypass on peer-authored content once triggered | reading (permission decorators + call chain, re-traced against source while writing this row) |
| D46 | `shared_inbox` | the fediseer exemption's body (`routes.py:730`) is a bare `...` (`Ellipsis`) expression statement — semantically inert, exactly like `pass` would be here. It does not return, log, or touch `bounced` (already `True` from line 718); its only effect is letting control fall out of the `try`/`except` into the shared bookkeeping (741) and dispatch (751+), which is precisely the same thing every other branch that reaches that point does by *not* returning. Named in this sub-project's own brief and confirmed still absent from the register until this row. **Not a functional defect**: `tests/test_inbox_gate_signatures.py::test_an_unsigned_chat_message_from_a_non_fediseer_actor_is_refused` (added in fix round 1, commit `3ca2eee1`) pins that dropping the actor-identity half of the `elif`'s condition is caught — re-confirmed while writing this row by reading the mutation record in the test file's own module docstring ("mutant 3 was re-run and killed too"), so the exemption is scoped correctly and is not silently over-broad. The finding is narrower than a first read of the mutation history suggests: **what remains is readability only** — a literal `...` with no comment explaining why the branch intentionally does nothing, in the body of a security-relevant exemption, is easy to misread as an unfinished stub on a future pass. | cosmetic — readability of a security-relevant branch, not a behavioural gap | reading + source inspection; the mutation-kill claim re-verified against the test file's own docstring, not re-run |
| D47 | `shared_inbox` | **FIXED 2026-08-29, by the D48 pair fix rather than at this call site.** Was: **under `ALLOWLIST_STRONG`, a peer on a non-allowlisted host skips the allowlist check entirely by sending `actor` as a JSON object instead of a string.** The check is `if g.site.allowlist_mode >= ALLOWLIST_STRONG and 'actor' in request_json: if not instance_allowed(furl(request_json['actor']).host): return '', 403` (`routes.py:673-675`). Three links, each verified against source: (1) `furl` parses strings only, so `furl({'id': 'https://blocked.example/u/x'}).host` is `None` — as is `furl('not-a-url').host`; (2) `instance_allowed(None)` returns `True` unconditionally (`app/utils.py:2302-2303`), so `not instance_allowed(...)` is `False` and the 403 never fires; (3) the actor still resolves, because `find_actor_or_create_cached` unwraps the dict — `if isinstance(actor, dict): actor = actor['id']` (`app/activitypub/util.py:341-342`) — after which the peer's own genuine HTTP signature verifies against its own real key and the activity is dispatched normally. A security control defeated by a shape change in peer-controlled JSON, at no cost to the peer: the dict form is legal ActivityPub. The author was aware `actor` may not be a string — `isinstance(request_json['actor'], str)` guards exist at `routes.py:686` and `:696` — but lines 674 and 705 carry no such guard. **Verified end to end, not inferred from the three links**: the same `signing_peer` on the same non-allowlisted host ('peer.example', no `AllowedInstances` row) posted to the real `/inbox` under `ALLOWLIST_STRONG` returns **403 with zero dispatches** when `actor` is the bare URI string, and **200 with `process_inbox_request` dispatched once** when `actor` is `{'id': <same URI>}`. Pinned as characterisation (today's behaviour, explicitly not endorsed) by `tests/test_inbox_gate_refusals.py::test_a_dict_shaped_actor_skips_the_strong_allowlist_check`, whose docstring states what a fix would flip. The fix taken was the deeper half this row named: `instance_allowed(None)` now returns `False`, so `not instance_allowed(...)` is True and the 403 fires. Preferred to reading the host out of the dict at this one call site because it closes the same shape at every other consumer of the pair. The characterisation test was flipped to a regression test asserting 403 with zero dispatches (`tests/test_inbox_gate_refusals.py::test_a_dict_shaped_actor_is_refused_under_strong_allowlist`), and that flip is how the change announced itself: it was the single failure in the full suite after the pair was fixed. | medium-to-high — peer-triggerable at will, a complete bypass of a security control rather than a partial one, but conditioned on the instance running in `ALLOWLIST_STRONG` mode, which most do not | reading + source inspection (all three links), then an end-to-end probe through the real route with a string-actor control |
| D48 | `instance_allowed` and `instance_banned` (`app/utils.py`), at every call site | **the instance-gating pair fails open on an empty host, and D47 is one of five places that reach it.** `instance_allowed(host)` opens `if host is None or host == '': return True`, and `instance_banned(domain)` opens `if domain is None or domain == '': return False`. Read together the pair is symmetric in the permissive direction: an absent host is *allowed* by the allowlist and *not banned* by the blocklist, so both federation modes admit it. Every caller was audited (section 6 below carries the table). The one that matters is **`validate_remote_actor` (`app/activitypub/actor.py:47`)**, which derives its host as `server, _ = extract_domain_and_actor(actor_url)` — and `extract_domain_and_actor` returns `('', '')` on a `urlparse` `ValueError` (`app/activitypub/util.py`, the `except ValueError` arm this campaign added). So an actor id that `urlparse` refuses — an unbalanced IPv6 bracket, a doubled `::`, an NFKC-confusable host — passes the instance gate in *both* modes rather than being refused by it. **This contradicts, in a narrow but load-bearing way, the comment that same `except ValueError` arm carries**: "every caller degrades to 'actor not found' from there." The end state may well be "actor not found", but the instance gate is not what produces it — the gate passes, and a later fetch failure is what actually stops the actor. That distinction is the finding, and it matters because **D32 already registers that the httpx-versus-`urlparse` gap is unproven**: nobody has exhibited a URL that `urlparse` refuses and httpx fetches successfully, and if one exists the gate is already open for it. **Not demonstrated end to end, unlike D47** — this row is source inspection across six call sites plus the two functions' own opening lines, with no probe through a live route. **PARTIALLY FIXED, and the demonstration this row said it lacked now exists.** The `validate_remote_actor` half was demonstrated end to end and then fixed under explicit authorisation, the first production change this campaign has made. Demonstration, one variable apart: with a `BannedInstances` row for 'banned.example', `instance_banned('banned.example')` is True and `instance_banned('')` is False; `validate_remote_actor('https://banned.example/u/x')` returned **False** and `validate_remote_actor('https://[banned.example/u/x')` returned **True** — one unbalanced bracket and a banned instance's actor is accepted by the check that exists to reject it. The fix guards `validate_remote_actor` before the gate: `if '://' in actor_url and not server: return False`. The `'://'` half is load-bearing and not defensive padding — `extract_domain_and_actor` returns `('', '')` for a webfinger handle as well, which `find_actor_or_create` accepts by design, so `if not server` alone would refuse every handle lookup in the codebase. Both directions are pinned by `tests/test_urlparse_valueerror_guards.py::TestAMalformedActorUrlDoesNotSkipTheInstanceGate` and both mutants were run: broadening the guard to `not server` fails the webfinger test, deleting it fails the malformed-URL test. **NOW FIXED IN FULL, 2026-08-29, including a second and more serious bypass this row had not found.** The pair itself fails closed: `instance_allowed('')` returns False and `instance_banned('')` returns True. The second bypass, found while fixing the first and needing no malformed input at all: `validate_remote_actor('alice@banned.example')` returned **True**, as did the `@`-prefixed spelling. An ordinary webfinger handle from a banned instance — the everyday form — was never checked against the ban, because `urlparse` reads a string with no scheme as pure path, so `extract_domain_and_actor` returns `('', 'alice@banned.example')` and the gate saw an empty host. `validate_remote_actor` now derives the host via `normalise_actor_string` before the gate, which is required *because* the pair fails closed: otherwise handles would be refused along with the malformed URLs. The `instance_banned` direction has a real cost, taken deliberately: roughly forty-five callers read `not instance_banned(instance.domain)` as an outbound delivery gate and `Instance.domain` is nullable, so a row with no domain now stops receiving deliveries rather than receiving them. Refusing to federate with a row whose identity is unknown is the safe direction, and such a row is already broken. Fixing the pair also closed **D47** as a side effect, which is the outcome that row predicted. Pinned by `tests/test_instance_domain_lookup.py` (both empty-value assertions inverted, with the reasoning recorded in their docstrings) and by `TestAMalformedActorUrlDoesNotSkipTheInstanceGate` in `tests/test_urlparse_valueerror_guards.py`. | medium — the failure direction is permissive in both federation modes; the most serious caller gated actor resolution generally and is now fixed, but the pair itself still fails open for the remaining four call sites | reading + source inspection at six call sites, **then an end-to-end demonstration on the ban path with a well-formed control**, then fixed and mutation-checked in both directions |

### 3. `replay_inbox_request`'s reachability, traced rather than assumed

D44 and D45's severities both turn entirely on who can reach
`replay_inbox_request`, so this was established before rating either, per
this sub-project's brief. There are exactly three callers in `app/`, found by
`grep -rn replay_inbox_request app/`:

| caller | route | gate | what `request_json` is |
|---|---|---|---|
| `app/admin/routes.py:1270`, `activity_replay` | `GET /activity_json/<int:activity_id>/replay` | `@login_required` + `@permission_required('change instance settings')` | a previously-logged `ActivityPubLog` row's own `activity_json`, chosen by the admin via `activity_id` |
| `app/dev/routes.py:206`, `tools_activitypub` | `POST /dev/tools/activitypub` | `@login_required` + `@permission_required('change instance settings')`, **and** `if not current_app.debug: abort(404)` at the top of the view | free-text JSON pasted into a form field by the admin |
| `app/main/routes.py:633`, `replay_inbox` | `GET /replay_inbox` | `@login_required` only — no permission check | **hardcoded to `{}`** in the current source (`app/main/routes.py:622`); the realistic example payload is left in a docstring-style triple-quoted string that is never assigned to anything |

**No path lets an unauthenticated peer invoke `replay_inbox_request`
directly.** Two of the three callers require the `change instance settings`
admin permission; the third requires only login but currently supplies a
fixed empty dict, which `replay_inbox_request`'s own first check (`routes.py:782`)
refuses immediately as a missing-fields failure — verified by reading the
function, not run as a test, since this sub-project's report-only remit
covers `app/activitypub/routes.py` and `app/main/routes.py`'s route body was
in scope only for this trace. As shipped, `/replay_inbox` is a no-op for any
caller, admin or not.

The one path that does carry real peer-authored content is the admin replay
tool: `ActivityPubLog.activity_json` rows include content a peer sent, and an
admin can choose any historical row and force it back through
`replay_inbox_request`, bypassing every protection `shared_inbox` applied the
first time that row was logged (including protections that may have caused
the row to be logged as a *failure* in the first place). That consequence is
significant enough that it is filed on its own, as D45, rather than folded
into D44's "structural divergence" framing — an earlier draft of this section
rated the whole finding low on the ground that no unauthenticated third party
can reach it, which understates the risk the admin path actually carries: the
*content* being trusted is still entirely peer-authored, and the bypass, once
triggered, is total, not partial. **D44 stays low** as the narrower claim it
now is — "these are the five structural differences, and no unauthenticated
peer reaches this function directly" — and **D45 carries the higher rating**
for the specific, reachable consequence: an admin action that processes
peer-authored content with no signature verification at all. The
`/replay_inbox` route's current hardcoded-empty payload means the one
login-only, non-admin path is inert rather than under-protected, so it
contributes to neither row's severity.

### 4. Converting D21, D24 and D35's reachability claims — upgrade only what the tests support

D21, D24 and D35 (registered in sub-project 2b and confirmed-by-test in
sub-project 3) each carry a reachability claim that traces up through
`process_inbox_request` to `shared_inbox`'s own inbox route. This sub-project
has now executed that entry point for the first time in the campaign, which
is exactly the condition under which those claims could, in principle, move
from reading to probe.

**They do not move, and the reason is mechanical rather than judgement-based.**
Every dispatch test in `tests/test_inbox_gate_dispatch.py` that reaches the
success path calls `_patch_dispatch_recorders`
(`tests/test_inbox_gate_dispatch.py:356-361`), which replaces
`app.activitypub.routes.process_inbox_request` and
`app.activitypub.routes.process_delete_request` with a `Recorder` —
`monkeypatch.setattr('app.activitypub.routes.process_inbox_request', inbox_recorder)`
and the same for `process_delete_request`. Every assertion in this
sub-project's success-path tests is against what the recorder captured
(`inbox_recorder.inline`, `.delayed`), never against anything the real
`process_inbox_request` body does. The real function is never called by this
suite.

So the honest statement is:

- **What this sub-project newly proves, by execution:** `shared_inbox` and
  `replay_inbox_request` really do hand off to `process_inbox_request` (or
  `process_delete_request` for a self-delete), with the correct arguments
  (`request_json`, `store_ap_json`), and really do choose between calling it
  directly and calling `.delay()` based on `current_app.debug` (`shared_inbox`
  only — `replay_inbox_request` always calls directly, per D44). That is a
  genuine, newly probe-backed fact about the dispatch boundary itself, and it
  did not exist as tested behaviour before this sub-project.
- **What this sub-project does not touch at all:** `process_inbox_request`'s
  own body, `process_announce_of_uri`, `resolve_remote_post`,
  `create_resolved_object`, `resolve_remote_post_from_search` — everything
  D21, D24 and D35 actually describe. None of it executes when the dispatch
  recorder stands in for `process_inbox_request`.
- **Therefore: D21, D24 and D35's reachability claims are unchanged by this
  sub-project.** They remain exactly as reading- or probe-backed as
  sub-projects 2b and 3 left them — sub-project 3's direct unit tests already
  probe each function's *own* behaviour, and sub-project 2b's call-site
  tracing (the `process_announce_of_uri` chain for D21, the `Move` handler for
  D24, the three call paths for D35) remains reading-only for how a peer's
  request actually arrives at each function through the live inbox. This
  sub-project reaches the dispatch call, not the handlers behind it, and
  claiming otherwise would repeat the exact error this campaign keeps
  correcting.

### 5. A test-harness gap, not a production defect

`block_outbound_http` (`tests/conftest.py:212-268`) is session-scoped,
autouse, and patches only `httpx` via respx. Its own docstring names three
known escapes: `urllib.request.urlopen`, `botocore`/`urllib3`, and `smtplib`
(see "`block_outbound_http` blocks httpx and nothing else" above). Task 6
found a fourth, previously undocumented one: **`pyld`'s default JSON-LD
document loader uses `requests`** (`jsonld.py:6547`), which respx's httpx
interception does not touch. The two LD-signature tests in
`tests/test_inbox_gate_signatures.py` that exercised `LDSignature.verify_signature`
made real outbound HTTPS calls to fetch JSON-LD contexts before this was
caught in review. The fix — a test-local static document loader installed via
`jsonld.set_document_loader`, the same pattern `app/main/routes.py:744`
already uses in production — is test-only; the verifier itself stays
unpatched and the signatures stay real, confirmed in the fix's re-review by
checking that the frozen contexts are complete JSON-LD bodies producing the
same URDNA2015 normalisation as a live fetch would, and that a
mismatched-keypair variant of the same fixture still fails signature
verification. This is filed here, next to the three gaps
`block_outbound_http`'s docstring already names, because it is the same kind
of gap and future sub-projects touching JSON-LD signing should know about it
before they hit real network calls the way this one did.

### 6. Every caller of the instance-gating pair, audited

D47's row named `instance_allowed(None)` returning `True` as "the deeper half,
shared by every other caller of that function", and left it there. This is that
audit. Six call sites, from `grep -rn 'instance_allowed(' app/`:

| caller | host argument | can it be empty? | consequence |
|---|---|---|---|
| `app/activitypub/actor.py:47`, `validate_remote_actor` | `server` from `extract_domain_and_actor(actor_url)` | **yes** — `('', '')` is returned on a `urlparse` `ValueError`, for an actor id the remote peer chooses | **demonstrated, then FIXED.** Both modes passed the gate; a banned instance evaded its ban by adding one `[`. Guarded now — see D48's row for the fix, the webfinger over-correction it had to avoid, and the two mutants |
| `app/activitypub/routes.py:674`, `shared_inbox` | `furl(request_json['actor']).host` | **yes** — `None` for any non-string or non-URL actor | **D47**, demonstrated end to end |
| `app/utils.py:2477` and `:2526` | `user.ap_domain`, a stored column | conditional — only if a `User` row was written with an empty `ap_domain`. D25 records that the Person/Service branch stores `ap_domain=server` unlowered from the same `extract_domain_and_actor`, so the shape is plausible; no such row was exhibited | unproven, and deliberately not rated as reachable on that basis |
| `app/community/util.py:38`, `search_for_community` | `server` from `address[1:].split('@')` | yes for an address like `!name@` | a **local user's** search input, not peer-supplied; low, and noted only for completeness |
| `app/activitypub/routes.py:62`, `webfinger` | `requesting_domain` | **no** — the walrus guard `if requesting_domain := requestor_domain():` skips the whole block when it is falsy | unaffected by the pair's behaviour. Worth one line anyway: skipping the block is *also* permissive, by explicit design rather than by the empty-host quirk |

Two things this audit did **not** establish, stated so nobody reads more into
the row than it earned. First, no end-to-end bypass was demonstrated for any
caller other than D47's — the `validate_remote_actor` case is source reading,
and the fetch that currently saves it was not probed. Second, whether a URL
exists that `urlparse` refuses and httpx accepts is exactly D32's open
question; this audit inherits that uncertainty rather than resolving it.

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

## A guard must be tested on the domain it claims to reject

D13's guard raised on the very input it existed to catch, and that survived two
reviews. It is the campaign's sharpest process finding, and the useful part is
that catching it needed no insight — only two mechanical checks the campaign
already had the discipline for:

1. **Run the guard's own trigger through it.** D13's registered description was
   "a malformed entry"; the test only ever supplied a dict missing a key, which
   is the single instance the fix trivially handled. Any non-dict entry — the
   rest of the domain the guard claimed to reject — reproduced the original
   defect immediately.
2. **Drop each half of a compound guard and require a distinct kill.** This is
   impossible to satisfy with one shape of test data, so the check forces the
   data to cover the domain. It had already caught a vacuous half in D30's first
   attempt, where the entries were strings and `"type" not in <a string>` is a
   legal substring test, so the `isinstance` half was load-bearing for nothing
   and the mutant dropping it survived with the suite green.

Both reviews accepted "the `KeyError` test passes" as proof the guard worked. It
only ever proved the guard handled the example that motivated it.

The rule, in one line: **`KEY not in entry` is a call into `entry`, so a
membership test is never a type test.** That sentence explains D13's original
miss, D30's surviving mutant, and the string-containing-`display_name` case
found while completing D13 — a legal substring test that passes the membership
check and then dies on `string indices must be integers`.

The corollary for reviewers: a mutation that kills is evidence about the input
you chose, not about the guard. Ask what else the guard claims to reject, and
whether anything tests that.

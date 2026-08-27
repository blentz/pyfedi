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

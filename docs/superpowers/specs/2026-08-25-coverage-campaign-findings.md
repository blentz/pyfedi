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

## Line numbers in this register are dated records, not maintained pointers — 2026-09-03

Every `file:line` citation below means *the line as it stood at the commit that
filed the entry*. It is not kept in step with the file afterwards, and it will
not be. **Locate the code an entry names by its content — the function name, the
guard, the header string it quotes — never by the number.**

The occasion for writing this down: sub-project 11 inserted two lines into
`app/activitypub/routes.py` — the `else: abort(400)` now at `:2233-2234`, its
Task 10, commit `e1951de5`, closing D188. Every citation in this register that
names a line at or after `:2233` in that file, and was filed before D187, was
measured against the pre-insertion file and so reads two lines low today. That
is roughly fifty rows — D47, D55, D57, D60, D62, D91, D113, D118–D141, D156,
D158–D164, D167–D172, D175, D176, D180, D181, D183–D185 — plus prose in the
sub-project 6 and sub-project 7 section openings.

**They are deliberately not being swept, and no future line-shifting change to
production code will trigger a sweep either.** Three reasons:

- A register entry is a dated record of what was true when the finding was made.
  That is what makes it evidence. Rewriting the number to match today's file
  converts a record into an assertion about today that nobody re-verified — the
  campaign's own dominant failure mode, a confident citation to a line that does
  not say what it is claimed to say.
- A sweep would have to be repeated after every future production change to the
  file, and each sweep is fifty chances to corrupt a citation that was correct.
- Every spec in this campaign already instructs its implementers to locate every
  code target by content, not by line number. This note codifies existing
  practice rather than introducing a rule.

**The one citation that was corrected, and the distinction that justifies it.**
D177's evidence column claimed a *current re-measurement* — it said, in effect,
"measured at this commit". A citation that asserts its own freshness and is
stale is a false claim, and false claims get fixed. A citation that is merely
old is a record, and records do not. That is the whole test: fix a stale line
number when the entry claims the number is current; leave it alone when the
entry is simply dated.

**A citation carries TWO claims and needs TWO checks — added 2026-09-04, earned
twice.** "*Code* inside *function* (`:N`)" asserts a line range **and** a
function attribution, and they fail independently. Sub-project 13 shipped two
false attributions whose line numbers were correct, and the check that had been
added covered only the range. Sub-project 14 then found a false attribution in
its own working ledger — the create-path twin of D240 was "relocated" from
`create_post_reply` to `notify_about_post_reply`, and the pattern occurs nowhere
in the latter (see D243's marked correction). **Apply the check to corrections
with MORE care than to originals, not less.** That is the sharper half of the
lesson: a correction carries more authority than the thing it corrects, so it
attracts less scrutiny — the sub-project 14 one survived a reviewer, a fix round
and a task boundary purely because it was labelled a correction. The mechanical
form of the check is cheap: `grep` for a string the code must contain, then walk
the module's `ast` to map the hit to its enclosing `def`, and compare **both**
results against what the entry says.

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
  as its deeper half and left unexamined. D49–D63 sub-project 5a (the inbox
  dispatcher's preamble, Announce unwrap, vote arms, and Flag/Move/QuoteRequest
  — see that section for the table). D64–D77 sub-project 5b (the Follow,
  Accept and Reject arms of the membership handshake — see that section for
  the table; D76–D77 are fixed, not merely registered, under this
  sub-project's own bounded authorisation). D78–D79 the fix-wave review that
  closed 5b out, correcting D77's and D68's rows and registering the two
  defects those corrections exposed — see that section for the table.
  D80–D96 sub-project 5c (the Delete, Lock, Add, Remove and Block arms of
  moderation — see that section for the table; D97–D102 are fixed, not
  merely registered, under this sub-project's own bounded authorisation,
  larger than 5b's — six defects across four commits rather than two).
  D103 the fix wave that closed 5c out, correcting D91's overstated scope
  in place and registering the site-ban `already_banned` guard as a dead
  branch consequent on D91 — see that section for the table.
  D91 was FIXED and D103 CLOSED on 2026-08-31, under direct user
  authorisation after 5c had already closed, taking no new numbers — see
  5c's section 6, which also corrects the one claim D91's row got wrong
  about its own recommended fix.
  D104 sub-project 5d (the `Undo` arm's Follow, Delete, Like/Dislike,
  Announce, ChooseAnswer, Lock and Block sub-types — see that section for
  the table; one defect, registered but not fixed — the sibling `Lock`
  arm's identical missing `/post/`-to-`PostReply` fallback, 5c's
  territory). D105–D109 are fixed, not merely registered — five
  defect-fixes across four commits, one commit fixing two at once (D105 and
  D106). Four of the five (D105–D107, D109) are under this sub-project's own
  bounded authorisation from the project owner; the fifth, D108, is a
  regression D105's own fix introduced and was authorised separately, by
  the controller, as its repair. D110–D112 were added by this sub-project's
  own whole-branch fix wave (2026-09-01): D110 fixed (the descendant-subtree
  `UPDATE`'s `db.session.execute` vs `session.execute` mismatch), D111 and
  D112 registered but not fixed.
  D113–D118 sub-project 5e (the Create/Update arm, plus the six statements
  left elsewhere in `process_inbox_request` — see that section for the
  table). D113–D114 are fixed, not merely registered — the feed-Announce
  crash surface and the poll-vote silence, two defects across three commits
  (one fix hoisted to a safer location after review). D115–D118 are
  registered but not fixed. D119–D120 5e's whole-branch review fix wave
  (2026-09-01, see that section's part 3): the Note-shaped poll-vote path's
  missing banned-instance check and neither federated poll-vote path
  enforcing `Poll.mode`, both registered but not fixed.
  D121–D131 sub-project 6 (`process_chat`, the private-message acceptance
  policy — see that section for the tables). D121–D123 are fixed, not
  merely registered — the unguarded `content` read, the unguarded `id`
  read, and the dead inner `is_local()` check, three defects across two
  commits (one commit fixing D121 and D122 at once, the same shape 5c's
  D98/D99 and D101/D102 and 5d's D105/D106 used). D124–D126 are registered
  but not fixed; D126 is explicitly judged unspecified behaviour rather
  than a defect. D127–D131 were added by this fix wave's own whole-branch
  review (2026-09-01, see that section's part 3): the Announce unwrap's
  unguarded inner `id` (dispatcher-wide pattern), the new `content`/`id`
  guards checking membership but not type (two defects), a behaviour
  change for non-`ChatMessage` objects reaching the second call site, and
  the pre-existing falsy-content blocked-phrase skip getting its own
  citable number. None of D127–D131 are fixed, same rule as every other
  whole-branch review finding in this register.
  D132–D140 sub-project 7 (`process_new_content`, the federated post/reply
  creation-and-edit delegate — see that section for the tables). D132–D135
  are fixed, not merely registered — the reply half's missing
  instance-admin disjunct, the silent reply-edit refusal, the post half's
  fall-off-the-end refusal (with the reply half's mirror log added in the
  same commit), and the in-place mutation of the caller's activity id, four
  defects across four commits, under this sub-project's own bounded
  authorisation. D136–D139 are registered but not fixed: D136 corrects the
  "`find_microblogging_community()` can return `None`" premise the spec and
  two task briefs carried forward, which does not hold once the function's
  own body is read; D137 is ruled KEEP-AS-IS rather than a defect; D138 and
  D139 are out-of-scope instances of the same write-through-a-shared-dict
  class D135's fix addressed only partially, by design. D140 is not a
  defect — a deliberate federation behavioural change introduced by D135's
  fix, ruled KEEP. D141 was added by this fix wave's whole-branch review
  (2026-09-02): a residual inner-guard asymmetry between the two halves'
  existing-content edit paths, registered but not fixed.
  D142–D152 sub-project 8 (`webfinger`/`process_webfinger_request`, the
  ActivityPub discovery endpoint — see that section for the table).
  D142–D144 are fixed, not merely registered — the Feed lookup's three
  missing visibility guards (both textually identical occurrences), the
  not-found status (200 to 404), and the malformed-resource status (200 to
  400), three defects across three commits, under this sub-project's own
  bounded authorisation. D145–D152 are registered but not fixed: D145 is
  the discarded query domain (a policy decision, not a bug); D146 is the
  `'acct:' in query` substring-not-prefix test, checked before the URL
  branch; D147 is `webfinger`'s dead `g.site` assignment, the same
  equivalent-mutant class as D95/D96/D103, and the reason full statement
  coverage of `webfinger` is unachievable through the route; D148 is a
  third lookup asymmetry (Community lowercases the actor, Feed does not);
  D149 is `object`/`type` shadowing builtins throughout the function; D150
  is the retained `description=` on D144's fix still saying "regex" where
  none exists; D151 is D143's fix removing a real 60-second negative cache
  in production (`CACHE_TYPE` defaults to `FileSystemCache` there), against
  an endpoint with no rate limit; D152 is D143 and D144's fixes rendering
  differently — 404 as HTML via PieFed's own handler, 400 as JSON via
  flask_smorest — because only the former has a code-specific error
  handler registered.
  D154–D166 sub-project 9 (`user_profile`, `community_profile`,
  `feed_profile`, the actor-profile endpoints — see that section for the
  table). D154–D155 are fixed, not merely registered — `user_profile`'s
  dead admin branch (a byte-for-byte no-op) and its two local lookups'
  missing `deleted`/`banned` guards, two defects across two commits (plus
  a follow-up commit closing a review finding against the second), under
  this sub-project's own bounded authorisation. D156–D166 are registered
  but not fixed: D156 is `user_profile` serving remote actors' documents
  while its siblings `abort(400)`; D157 is the deliberately-unguarded
  remote lookup even after D155's local-lookup fix; D158 is a three-way
  ban-guard asymmetry among the three endpoints' local vs. remote lookups,
  cross-referencing D153; D159 is `preferredUsername` reflecting the
  caller's casing in all three, and `id` doing so too in `community_profile`
  and `feed_profile` but not in `user_profile`, which builds its `id` from
  the resolved row's own stored `ap_public_url`/username instead; D160 is three different
  `Cache-Control`
  max-ages; D161 is `feed_profile` never setting `Vary: Accept`; D162 is
  `community_profile`'s not-found path branching on authentication, whose
  two authenticated arms are the only statements left uncovered in any of
  the three functions and are unreachable in this harness for a stated
  reason; D163 is only `user_profile` accepting `HEAD`; D164 is
  `is_activitypub_request`'s byte-identical dead-copy duplication,
  confirmed independently of sub-project 8's Task 1 finding; D165 (added by
  this fix wave's whole-branch review) is `resolve_remote_handle`'s own
  AP-Accept and `'@' not in actor` guards being uncovered despite the
  original report describing them as pinned; D166 (also added by the fix
  wave) is webfinger's User lookup matching `alt_user_name` as well as
  `user_name` while `user_profile`'s local lookup matches only `user_name`.
  D167–D186 sub-project 10 (the nine ActivityPub collection endpoints —
  `community_outbox`, `community_featured`, `community_moderators_route`,
  `community_followers`, `user_followers`, `feed_outbox`, `feed_following`,
  `feed_moderators_route`, `feed_followers` — see that section for the
  tables). D167–D169 are fixed, not merely registered: three
  remotely-reachable HTTP 500s on `GET /f/<unknown>/moderators`,
  `/outbox` and `/following`, by **two different mechanisms** — a `if feed
  is not None:` with no `else` (the view returns `None` and Flask raises
  `TypeError`) versus no `None` check at all (`feed.public` dereferenced,
  `AttributeError`) — three defects across three commits, under this
  sub-project's own bounded authorisation. D170–D186 are registered but
  not fixed: D170 is a FOURTH crash in `feed_moderators_route`, on the
  found-feed path, where a null `Feed.user_id` makes `.get(None)` return
  `None` and `moderator.ap_profile_id` raise; D171 is the cartesian-product
  join in `feed_outbox` and `feed_following`, real in the SQL but **masked
  entirely** by legacy `Query.all()`'s automatic entity deduplication, a bug
  waiting on an unrelated migration to `select()`; D172 is `feed_outbox`
  publishing the `local_only`/`private` communities `feed_following`
  withholds, despite a comment calling them equivalent; D173 is
  `community_outbox`'s `totalItems` being the page size (50), not the
  collection size; D174 is `community_moderators` synthesising a
  never-persisted `CommunityMember`, so the collection publishes a phantom
  moderator with no database row; D175 is `community_followers` and
  `feed_followers` reporting a real `totalItems` beside a permanently empty
  `items` while `user_followers` populates its own; D176 is the four feed
  lookups having neither a `banned` nor a `public` guard where the community
  and user lookups filter `banned=False`, cross-referencing D148 and D158;
  D177 is none of the nine checking `is_activitypub_request()`, unlike the
  three actor-profile and four content-object endpoints in the same file;
  D178 is only `user_followers` setting `Vary: Accept`, on the one endpoint
  whose body does not vary by it; D179 is `community_featured` ignoring the
  post-status filter its sibling applies, so one post is excluded from the
  outbox and published in featured; D180 is `community_featured` setting no
  `Cache-Control` while the other eight set four different values; D181 is
  `user_followers` 404-ing on a null `ap_followers_url` that local
  registration never populates; D182 is that endpoint's block filter running
  the opposite direction from its own comment; D183 and D184 are two more
  cross-endpoint rendering asymmetries (`public_url()` vs `ap_profile_id`;
  caller-casing echoed into `id`, the D159 class on two more endpoints);
  D185 is `outbox` declaring two different collection types on two
  endpoints; D186 is a test-suite finding, not a production defect — four
  filter clauses that no mutation could kill until this sub-project wrote
  the tests for them: `community_outbox`'s three sticky-side duplicates and
  `user_followers`' `banned=False`. D187-D198 were taken by sub-project 11, the six
  ActivityPub content-object and activity-log endpoints -- D187 is
  `activity_result` returning this instance's internal exception text to any
  unauthenticated caller, the most serious finding in that slice; D188 is the
  fourth instance of the D167-D169 crash class, fixed; D189-D191 are three
  visibility-guard gaps across `post_replies_ap`, `post_ap_context` and
  `comment_ap`; D192-D196 are two dead `HEAD` branches, a write-on-GET, an
  inert instance-block guard, and two `Cache-Control` findings; D197 and D198
  are test-suite findings, not production defects. D199 was taken by that
  sub-project's final whole-sub-project fix wave: `post_ap` has no `deleted`
  guard, so a soft-deleted post's full `Page` JSON is served where the same
  post's `/context` 404s -- the fourth cell of the slice's `deleted`-guard
  row, and the one nothing else disposed of.
  D200-D212 sub-project 12 (the six moderation and ban-removal functions in
  `app/activitypub/util.py` -- `delete_post_or_comment`,
  `restore_post_or_comment`, `site_ban_remove_data`,
  `community_ban_remove_data`, `ban_user`, `unban_user` -- see that section for
  the tables). D201 is fixed, not merely registered: `site_ban_remove_data`
  assigned `blocked.reply_count = 0` to a column `User` does not declare, so
  site-banning a user never zeroed their real reply counter, one commit
  (`25de721d`) under this sub-project's own bounded authorisation. D200 and
  D202-D210 are registered but not fixed: D200 is the delete/restore cycle
  losing `community.post_reply_count` and `post.reply_count_cross_posted` on
  every round trip, the most consequential finding in the slice and the only
  one proved by a round-trip test rather than by reading; D202 is
  `restore_post_or_comment` taking none of the seven redis locks
  `delete_post_or_comment` wraps the same counter mutations in; D203 is
  restore's cross-post guard dropping delete's second conjunct; D204 is
  `unban_user`'s instance branch writing no modlog entry where the other three
  ban/unban branches do; D205 is `ban_user`'s existing-row guard having
  different scope in its two branches, so a re-ban is a silent no-op in a
  community and a duplicate modlog entry instance-wide; D206 is both
  ban-removal functions omitting the `reply_count_cross_posted` decrement that
  `delete_post_or_comment` performs on the same rows, a third site of D200's
  counter; D207 is the two ban-removal functions splitting between
  `db.session.query(...)` and the legacy `.query`, cross-referencing D171;
  D208 is the four-disjunct authorisation guard being copied between delete and
  restore rather than shared; D209 is the `purge_cdn` call-site asymmetry being
  cosmetic rather than behavioural -- a **falsified spec claim**, registered in
  its corrected form; D210 is `Post.post_reply_count_recalculate`, a
  never-called method writing the same class of undeclared attribute D201
  fixed. D211 and D212 are test-suite findings, not production defects.
  D213-D231 sub-project 13 (the three mirrored actor-refresh Celery tasks in
  `app/activitypub/util.py` -- `refresh_user_profile_task`,
  `refresh_community_profile_task`, `refresh_feed_profile_task` -- see that
  section for the tables). **Five are fixed**, in five commits under that
  sub-project's own bounded authorisation, and every one of them was a
  remotely-triggerable crash a peer could provoke: D213 is the missing
  `instance_id` conjunct that made a NULL-instance community or feed raise
  `AttributeError` where the user task's guard has always been right
  (`1ba9f0a2`); D214 is the unguarded `.json()` on the actor document in the
  same two tasks, which also gains them the instance failure-counting the user
  task already did (`84be0559`); D215 is the feed task fetching
  `ap_following_url` with no check that it was set, four of five collection
  fetches gated and this one not (`4cd0042c`); D216 is that same fetch checking
  neither status code nor decode where five sibling fetches check status
  (`ce849451`); D217 is four collection guards subscripting `['type']` or
  `['items']` on a peer-controlled document with no membership check, the
  featured guard nine lines away being the correct spelling (`5113324a`).
  D218-D228 are registered but not fixed. D218 and D219 are two whole
  **families** of the same class, catalogued with verified line numbers as the
  spec for a follow-on slice: the actor documents' own required keys
  (`preferredUsername`, `name`, `publicKey`) read unguarded at six sites, and
  the list-entry subscripts, empty-list indexing and three mirrored-pair
  asymmetries. **The line between fixing and registering is the rule worth
  reusing**: a defect is fixed under a bounded authorisation when the correct
  spelling already exists in the file and the fix is mechanical, and registered
  when it would require choosing new behaviour for a case the codebase has never
  handled. D220 is the user task's bare `except:` -- which is also what made one
  of its own guard's conjuncts untestable; D221, D222 and D223 are the
  signed-GET fallback, the `is_local()` check and the `activity_json` parameter
  each existing in exactly one of the three copies; D224, D225 and D226 are
  three inline worker costs, the `randint(3, 10)` retry sleep, the
  `time.sleep(0.5)` per collection entry, and `find_actor_or_create`'s
  `create_if_not_found=True` fan-out; D227 is the user task fetching nothing
  beyond its actor document, which is what explains the shape of every other
  entry; D228 is a deliberate, flagged asymmetry D217's own fix introduced.
  D229-D231 are test-suite findings, not production defects, and D229 is the
  most reusable thing in the slice: mutating a whole guard to `if True:` is a
  site-level proof, not a conjunct-level one, and the gap is self-concealing
  because the site-level mutant does die.
  D232-D235 were added by sub-project 13's **final fix wave**, after the
  whole-branch review of D213-D231; see subsection 6 of that section. D232 is
  the following-collection fetch sending no `Accept` header -- the only one of
  the trio's twelve `get_request` calls that omits it, and the one D216's new
  decode guard turned from a loud crash into a permanent silent failure to
  sync a feed. D233 is the kbin `moderators` arm missing the `isinstance(...,
  str)` check its `attributedTo` neighbour makes, in both the community and
  feed tasks. D234 is that every collection guard tests key **membership** and
  none tests container **type**, so a JSON string is iterated byte by byte.
  D235 is the residual: **the spec's first success criterion, full statement
  coverage of the three functions, was not met** -- 124 statements uncovered,
  measured per function, concentrated in the document-application bodies, and
  named as the scope for a follow-on slice. **Two of D232-D234 came back from
  source different from how the review described them, and both corrections
  are recorded in the entries rather than applied silently**; that is the same
  discipline D209 records for a falsified spec claim.
  **Next free number: D297.** D232-D235 were taken by sub-project 13's final
  fix wave; D236-D251 by sub-project 14 and D252-D259 by that sub-project's own
  final fix wave; D260-D269 by sub-project 15 and D270-D273 by that
  sub-project's own final fix wave; D274-D282 by sub-project 16, with
  **D283 by that sub-project's own final fix wave**; and **D284-D296 by
  sub-project 17**, whose section is the last in this file.
  If you take it, say so here in the change that takes it.

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
| D32 | `actor_json_to_model` | the host gate `host_of(activity_json['id']) != host_of(f'//{server}')` is sound only while `server` is non-empty: `host_of` degrades an unparseable string to `''`, and `'' != ''` is False, so two failed parses pass the gate. Four of the five call sites keep `server` non-empty; the fifth, `create_actor_from_remote` in `app/activitypub/actor.py`, does not — on its `https://`/`http://` path it takes `server` from `extract_domain_and_actor`, which returns `('', '')` on a `urlparse` `ValueError`, and then fetches with `actor_address`, a different variable, so nothing ever exercises `server`. **Reachability is UNPROVEN and is not claimed:** it needs httpx to accept and successfully fetch a URL that Python's `urlparse` refuses. Nobody has exhibited such a URL. **RESOLVED AND FIXED 2026-08-29. The open question is settled affirmatively: such a URL exists, and it is not exotic.** The shape is USERINFO, not the netloc itself. `urlparse` validates the whole netloc including userinfo and rejects the string; httpx splits userinfo off first and validates only what remains, which is an ordinary registrable host. Measured: `urlparse('https://[@banned.example/u/alice')` raises `ValueError: Invalid IPv6 URL` while `httpx.URL(...).host` is `'banned.example'` — so httpx sends the request to the peer's real server while every `urlparse`-derived value in this codebase is `''`. An NFKC-confusable in the userinfo (`https://℀@banned.example/...`) does the same and reads as far less obviously malformed. **Two independent stops now exist and both are wanted.** First, `validate_remote_actor` refuses any `://` URL whose host it cannot derive (the D48 fix, same day), so `create_actor_from_remote` — the one caller that could deliver an empty `server` — is never reached; this holds with no `BannedInstances` row present, which is what proves it is the guard rather than the ban. Second, the gate itself now refuses rather than compares: `if not id_host or id_host != server_host`. **One operand, not two.** The first version of the fix also tested `not server_host`; it was removed after measurement, not reasoning. If exactly one side is empty the inequality already refuses, so both-empty is the only case needing a guard and either guard alone catches it — the two-operand form left BOTH single-operand mutants surviving, which is D40's shape and was not going to be committed one section below where D40 is filed. With one operand both mutants die: dropping `not id_host` fails the D32 test, dropping the inequality fails 4. `host_of`'s docstring is corrected: it recorded this gate as discharging its obligation by caller, and that reliance was misplaced — `create_actor_from_remote` takes `server` from `extract_domain_and_actor`, which returns `('', '')` on exactly the `ValueError` `host_of` swallows. The obligation is no longer load-bearing for this gate. Pinned by `tests/test_ap_actor_json_person.py::TestTwoFailedParsesDoNotSatisfyTheGate` (three tests: the both-empty case, plus the two asymmetric controls that already held and that pin why the pair is the dangerous one) and by `tests/test_urlparse_valueerror_guards.py::TestHttpxFetchesHostsUrlparseRefuses`, which parametrises four such URLs across four facts each — `urlparse` raises, httpx reads the real host, this codebase's own helpers see `''`, and `validate_remote_actor` refuses. That last file is the rot guard: any httpx or CPython release that changes which library accepts these strings fails it, and that is a signal to re-check both gates rather than a test to relax. | **low-to-medium — the gap was real and the reachability is now proven, but the only caller that could deliver an empty `server` was independently closed the same day; the gate fix is defence in depth for the next caller** | tracing `host_of`'s stated caller obligation to each call site while correcting its docstring, **then probing httpx against `urlparse` directly to settle the open question**, then fixing and mutation-checking both operands |
| D33 | `actor_json_to_model`, Group | the branch's `except KeyError` wraps a `Community(...)` call whose keyword arguments include `instance_id=find_instance_id(server)` — which **commits an `Instance` row** when the peer is new — followed, later in the same argument list, by `content_retention=current_app.config['DEFAULT_CONTENT_RETENTION']`. Keyword arguments evaluate in source order, so a missing config key raises `KeyError` after the Instance commit has landed, and the handler swallows it and returns `None`. The caller sees "malformed peer document"; the truth is a misconfigured deployment, and a sparse Instance row plus a `new_instance_profile` fetch are left behind. Deployment error rather than peer input, which is why it is filed separately from D30/D31 rather than as another instance of the shape. **FIXED 2026-08-30, and both halves were demonstrated before being fixed** rather than argued from source: with `DEFAULT_CONTENT_RETENTION` deleted from the config, `actor_json_to_model` on a Group document for an unseeded peer returned **None**, left **1** `Instance` row for `peer.example`, and created **0** Communities. The fix is two changes. (1) The config read moves above the `try`. It is the only argument in that list that can raise and is not peer data, so catching it in a handler that logs `while parsing <the peer's JSON>` names the wrong party; and `config.py` always sets the key (`int(os.environ.get(...) or -1)`), so a deployment missing it is broken in a way its operator needs to see. **Deliberate behaviour change: it now propagates instead of returning None.** (2) `instance_id` is assigned after the construction succeeds rather than inside the argument list, so the commit and the `new_instance_profile` fetch are unreachable until there is a Community to attach them to — which holds no matter what is added to the list later, where reordering alone would not. **The two halves are redundant against each other today, and that was measured rather than assumed.** With only (1) applied, moving `find_instance_id` back mid-list leaves every other test in the class passing: once the config read is gone, nothing left in the argument list can raise, so the call's position stops being observable. Rather than report the mutant as surviving, the ordering is pinned directly — a document that raises when asked about `postUrlType`, the last argument and unambiguously downstream of the old position, must leave no Instance row. **One dead end worth recording**, because it is the sort of test that looks discriminating and is not: the first version of that pin patched `utcnow` to raise on its second call. It did not discriminate. `find_instance_id` calls `utcnow` itself when building the sparse Instance, so the counter fired inside it, ahead of its own commit, and the mutant survived. A raise sourced from the peer document cannot be short-circuited that way. Scope confirmed to the Group branch only: the Person and Feed branches carry the same `find_instance_id` call in the same mid-list position, and every argument after theirs is a guarded read or a literal. Pinned by `tests/test_ap_actor_json_group.py::TestASideEffectingArgumentIsNotEvaluatedBeforeTheOnesThatCanFail` (5 tests: the two config halves, the ordering pin, plus two controls — the happy path still creates the row, and a peer-caused refusal still creates none). Three mutants, all dying: config read back inside the try (2 failures), `find_instance_id` back in the argument list (1), `instance_id` never assigned (1). | low — needs a deployment missing a key `config.py` always sets, so it is a latent misdiagnosis trap rather than a live hole; the cost when it fires is an operator sent to audit an innocent peer document while sparse Instance rows and background fetches accumulate | the same read; `find_instance_id` is easy to miss as a writing call because it is spelled as a lookup — **then demonstrated end to end, fixed, and mutation-checked in three directions** |

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
owner authorisation. **D32 was fixed on 2026-08-29** after its open reachability question was settled affirmatively, and **D33 on 2026-08-30** — see their rows. Nothing in this section remains open. Both fixes are
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
| **D35** | `create_resolved_object` **and** `resolve_remote_post_from_search` | **no remote reply can be created by either resolver.** Both synthesise their activity as `{'id': ..., 'object': post_data}` with no `'type'` key; `Post.new` reads that key defensively, `PostReply.new` reads `request_json['type']` unguarded, so it raises `KeyError('type')`, `create_post_reply` swallows it, and the resolver returns None. Every reply arriving by Announce, by microblog boost, or by the alpha API is silently dropped. **FIXED 2026-08-29.** `PostReply.new` now guards its read exactly as `Post.new` has always guarded its own: `if request_json and 'type' in request_json and request_json['type'] == 'Update'` (`app/models.py`). Deliberately not fixed by adding `'type'` to the synthesised activity: in `create_resolved_object` that dict is built before `activity` may flip from `'update'` to `'create'`, so a build-time `'type'` would be wrong on the fallback path. TDD, red first: the new tests failed with `AttributeError: 'NoneType' object has no attribute 'id'` before the guard and pass after. Six characterisation tests flipped on the fix — exactly the six written to flip when it landed, and no others — and each was rewritten to assert the correct behaviour rather than deleted. A third possibly-affected path was noticed and is NOT covered: `app/community/util.py:272` passes a NodeBB `reply_data` whose `'type'` is not guaranteed, inside its own `try`. | **medium-high — silent, total for replies, three call paths** | test in both files, plus the APLOG message pinned as `'type'`; after the fix, two working-path tests per resolver and the post-branch control kept as the asymmetry's evidence |
| **D36** | both resolvers | the reply branch's enrichment and, in `resolve_remote_post_from_search`, the `return object.post` contract were unreachable while D35 stood. Not defects themselves; recorded so that fixing D35 would be known to also un-dead three code paths no test could then exercise. **CLOSED 2026-08-29 by the D35 fix, and the prediction held exactly.** Both resolvers now measure complete: `create_resolved_object` 60/60 statements and 44/44 arcs (was 55/60 and 41/44 — the five statements and three arcs were the one dead region this row named), `resolve_remote_post_from_search` 73/73 and 46/46 (was 45/46, the missing arc being `if not in_reply_to:` taking its False path). Both mutants this row predicted would start dying do: deleting the reply branch's `else: activity = 'create'` now fails `test_an_update_for_a_reply_that_does_not_exist_falls_back_to_create`, and `return object` unconditionally now fails `test_a_reply_resolves_to_its_parent_post`. One behaviour was found only because the region became reachable: `PostReply.new` bumps the PARENT's `last_active` to local now, so `resolve_remote_post_from_search`'s `not in_reply_to` guard stops the peer's timestamp reaching that column but not the column moving. | informational | branch-arc measurement, then re-measurement and two mutants after the fix |
| **D37** | both resolvers | `posted_at` is `timestamp without time zone` and the peer's raw string is assigned to it, so Postgres **discards a non-zero offset rather than converting it**. A peer publishing at `00:00+05:00` is recorded as `00:00` — five hours late — and `last_active` with it, which is what orders community listings. Stores cleanly; simply wrong. | low-medium — silent, affects ordering | test (`TestAPublishedOffsetIsDiscardedNotConverted`) |
| **D38** | `create_resolved_object`, `resolve_remote_post_from_search` | an `attributedTo` that is a single embedded object — `{'type': 'Person', 'id': ...}`, ordinary ActivityStreams — matches neither the string arm nor the list arm, so the author is never found and the document is refused. The same object inside a one-element list is accepted. **`verify_object_from_source` already handles it**, so the fix exists in-file. | low-medium — peer-triggerable availability | test in both files; the fixed copy's dict arm |
| **D39** | `resolve_remote_post_from_search` | `post_data['id']` in the second existence check is an unguarded read on a peer document, sitting between two guards that use the `'key' in ...` idiom correctly. A document without `'id'` raises `KeyError` out of the function; on the `Move` path a peer chooses that document. | low — availability | test (`test_a_document_with_no_id_raises_keyerror`) |
| **D40** | `resolve_remote_post_from_search` | the `and nodebb` conjunct in the `find_community` fallback does no work. `topic_post_data` diverges from `post_data` only in the OrderedCollection branch, which is the only place `nodebb` becomes True; with `nodebb` False the fallback would repeat a lookup that has already returned None. | informational — code quality | surviving mutant, with the divergence argument |

D35 was the one worth acting on soonest, and it was fixed on 2026-08-29. It is
not a parsing subtlety: it was an entire class of federated content that
neither resolver could ingest, failing silently, with the traceback swallowed
by a bare `except Exception` and no log line unless `LOG_ACTIVITYPUB_TO_DB`
happened to be on. The bare `except Exception` in `create_post_reply` is still
there and is still what made a total failure invisible for as long as it was;
it is not registered as a separate row, but it is the reason the defect's cost
was measured in months rather than in one traceback.

### 7. What the mutation runs cost, and what they caught that reading did not

52 mutants across the six tasks. Five survived at the time and are reported
rather than chased — D34 and D40 above, the reply-branch fallback and the
return shape that D36 covers, and `activity` forced to `'update'`, which the
update path's fallback to create makes unobservable. **Two of the five now die**
without a test having been written to chase either: fixing D35 made the
reply-branch fallback and the return shape observable, which is the strongest
evidence available that D36's unreachability claim was correct rather than a
gap dressed up as an explanation. Three survive today.

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
| D48 | `instance_allowed` and `instance_banned` (`app/utils.py`), at every call site | **the instance-gating pair fails open on an empty host, and D47 is one of five places that reach it.** `instance_allowed(host)` opens `if host is None or host == '': return True`, and `instance_banned(domain)` opens `if domain is None or domain == '': return False`. Read together the pair is symmetric in the permissive direction: an absent host is *allowed* by the allowlist and *not banned* by the blocklist, so both federation modes admit it. Every caller was audited (section 6 below carries the table). The one that matters is **`validate_remote_actor` (`app/activitypub/actor.py:47`)**, which derives its host as `server, _ = extract_domain_and_actor(actor_url)` — and `extract_domain_and_actor` returns `('', '')` on a `urlparse` `ValueError` (`app/activitypub/util.py`, the `except ValueError` arm this campaign added). So an actor id that `urlparse` refuses — an unbalanced IPv6 bracket, a doubled `::`, an NFKC-confusable host — passes the instance gate in *both* modes rather than being refused by it. **This contradicts, in a narrow but load-bearing way, the comment that same `except ValueError` arm carries**: "every caller degrades to 'actor not found' from there." The end state may well be "actor not found", but the instance gate is not what produces it — the gate passes, and a later fetch failure is what actually stops the actor. That distinction is the finding, and it matters because **D32 already registers that the httpx-versus-`urlparse` gap is unproven**: nobody has exhibited a URL that `urlparse` refuses and httpx fetches successfully, and if one exists the gate is already open for it. **Not demonstrated end to end, unlike D47** — this row is source inspection across six call sites plus the two functions' own opening lines, with no probe through a live route. **PARTIALLY FIXED, and the demonstration this row said it lacked now exists.** The `validate_remote_actor` half was demonstrated end to end and then fixed under explicit authorisation, the first production change this campaign has made. Demonstration, one variable apart: with a `BannedInstances` row for 'banned.example', `instance_banned('banned.example')` is True and `instance_banned('')` is False; `validate_remote_actor('https://banned.example/u/x')` returned **False** and `validate_remote_actor('https://[banned.example/u/x')` returned **True** — one unbalanced bracket and a banned instance's actor is accepted by the check that exists to reject it. The fix guards `validate_remote_actor` before the gate: `if '://' in actor_url and not server: return False`. The `'://'` half is load-bearing and not defensive padding — `extract_domain_and_actor` returns `('', '')` for a webfinger handle as well, which `find_actor_or_create` accepts by design, so `if not server` alone would refuse every handle lookup in the codebase. Both directions are pinned by `tests/test_urlparse_valueerror_guards.py::TestAMalformedActorUrlDoesNotSkipTheInstanceGate` and both mutants were run: broadening the guard to `not server` fails the webfinger test, deleting it fails the malformed-URL test. **NOW FIXED IN FULL, 2026-08-29, including a second and more serious bypass this row had not found.** The pair itself fails closed: `instance_allowed('')` returns False and `instance_banned('')` returns True. The second bypass, found while fixing the first and needing no malformed input at all: `validate_remote_actor('alice@banned.example')` returned **True**, as did the `@`-prefixed spelling. An ordinary webfinger handle from a banned instance — the everyday form — was never checked against the ban, because `urlparse` reads a string with no scheme as pure path, so `extract_domain_and_actor` returns `('', 'alice@banned.example')` and the gate saw an empty host. `validate_remote_actor` now derives the host via `normalise_actor_string` before the gate, which is required *because* the pair fails closed: otherwise handles would be refused along with the malformed URLs. The `instance_banned` direction has a real cost, taken deliberately: roughly forty-five callers read `not instance_banned(instance.domain)` as an outbound delivery gate and `Instance.domain` is nullable, so a row with no domain now stops receiving deliveries rather than receiving them. Refusing to federate with a row whose identity is unknown is the safe direction, and such a row is already broken. Fixing the pair also closed **D47** as a side effect, which is the outcome that row predicted. Pinned by `tests/test_instance_domain_lookup.py` (both empty-value assertions inverted, with the reasoning recorded in their docstrings) and by `TestAMalformedActorUrlDoesNotSkipTheInstanceGate` in `tests/test_urlparse_valueerror_guards.py`. | medium — the failure direction was permissive in both federation modes; both halves are fixed now, the pair fails closed at all six call sites, and the residual risk moves to the deliberate `instance_banned` cost recorded above rather than to any remaining bypass | reading + source inspection at six call sites, **then an end-to-end demonstration on the ban path with a well-formed control**, then fixed and mutation-checked in both directions |

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

**Both were resolved on 2026-08-29, and the second resolved against the
audit's hope.** The `validate_remote_actor` case was demonstrated end to end
and fixed (see D48). And such a URL does exist — `https://[@banned.example/...`,
where the offending character sits in the USERINFO — so the fetch that "would
have saved it" would in fact have succeeded, against the peer's real host. The
audit was right to refuse to claim safety it had not measured: the assumption
it declined to make was false.

## Sub-project 5a: the inbox dispatcher's preamble, Announce unwrap, vote arms, and Flag/Move/QuoteRequest

`docs/superpowers/specs/2026-08-30-coverage-inbox-dispatch-5a-design.md`, on
branch `blentz`. Eight test-writing tasks covered
`process_inbox_request`'s own body — the function sub-project 4's gate hands
off to and then never executes — plus its four vote delegates
(`process_upvote`, `process_downvote`, `process_poll_vote`,
`process_question_answer`) and its Flag, Move and QuoteRequest arms. This is
the first sub-project in the campaign to call `process_inbox_request`
directly, and (via one task) the first to reach it through a real signed
HTTP request without patching it away. 67 tests across four files:
`tests/test_inbox_dispatch_preamble.py` (17), `tests/test_inbox_dispatch_announce.py`
(9), `tests/test_inbox_dispatch_votes.py` (29), `tests/test_inbox_dispatch_misc.py`
(12). This is report-only, per the sub-project's own remit: nothing in
`app/` was touched by any of the eight tasks, and nothing is touched here.

### 1. Whole-unit coverage, and every gap explained

Measured against this sub-project's own suite:

```bash
./run_tests.sh tests/test_inbox_dispatch_preamble.py tests/test_inbox_dispatch_announce.py \
  tests/test_inbox_dispatch_votes.py tests/test_inbox_dispatch_misc.py \
  -q --cov=app.activitypub.routes --cov-report=json
```

67 passed. `app/activitypub/routes.py` as a whole (1813 statements, 890
branches): 369/1813 statements, 127/890 branches, **18.35% blended
`percent_covered`** — up from the 14.76% the branch carried before this
sub-project (per the plan's own baseline reading), because this is the first
sub-project to exercise `process_inbox_request`'s body at all rather than
stopping at `shared_inbox`'s dispatch call to it.

By intersecting each span's line range against `coverage.json`'s
`executed_lines`/`missing_lines`/`executed_branches`/`missing_branches`, the
same method sub-projects 2a and 4 used:

| span | unit | statements | branches |
|---|---|---|---|
| 839-934 | preamble | 68/71 executed | 2 missing arcs |
| 1328-1343 | the four vote arms | 8/12 executed | 2 missing arcs |
| 2387-2410 | `process_upvote` | 20/20 executed | 0 missing arcs |
| 2413-2433 | `process_downvote` | 10/18 executed | 6 missing arcs |
| 2436-2460 | `process_poll_vote` | 21/21 executed | 0 missing arcs |
| 2463-2496 | `process_question_answer` | 23/24 executed | 2 missing arcs |
| 1344-1355 | Flag | 8/8 executed | 0 missing arcs |
| 1571-1589 | Move | 14/14 executed | 2 missing arcs |
| 1880-1884 | QuoteRequest | 5/5 executed | 0 missing arcs |
| 1885-1889 | the except/finally | 4/4 executed | 0 missing arcs |

Every gap, explained rather than left as a remainder:

1. **Preamble, line 860 (`pass`) and its guarding branch (859→860).** The
   `s.rimu.geek.nz` breakpoint hook (registered below as D53) is never
   triggered. No test sends an actor id starting with that string, and it
   would prove nothing if one did — the arm is a bare `pass`, so "covering"
   it means confirming a no-op is a no-op. Legitimately left untested.
2. **Preamble, lines 888-889 and their guarding branch (878→888).** The
   final `else` of the Community-actor dispatch (`'Unexpected activity from
   Group'`) is never reached. Tasks 1-8 drive a Community (Group) actor
   through `Add`/`Remove` (877), `Update`/`Group` (879-880),
   `Update`/`OrderedCollection` (881-882) and `Update`/anything-else
   (884-886) — every arm of the `elif` at 878 — but no test sends a
   Community actor an activity type that is neither `Add`, `Remove` nor
   `Update` at all, which falls past the `elif` itself to 888. That is
   not the only way to reach 888, though: an `Update` whose object
   simply lacks a `'type'` key (e.g. `object={}`) also falls there,
   since routes.py:878's second conjunct (`'type' in
   request_json['object']`) fails and the `elif` as a whole is false.
   A real, if narrow, scope gap in this sub-project's own test matrix,
   not a defect: the code at 887-888 is unremarkable, it is simply
   never exercised.
3. **The four vote arms, lines 1337-1338 and 1341-1342 (branches 1336→1337,
   1340→1341).** Only two of the four dispatch arms are driven through the
   full `dispatch()` helper: Task 5's `test_like_and_emojireact_dispatch_to_process_upvote`
   and `test_dislike_dispatches_to_process_downvote` send real `Like`/
   `EmojiReact`/`Dislike` activities through `process_inbox_request` (with
   the delegate itself monkeypatched to a recorder), which is why lines
   1328-1334 read as executed. Task 6's `process_poll_vote` and
   `process_question_answer` tests, by contrast, call those two functions
   **directly** — `PollVote` and `ChooseAnswer` activities are never sent
   through `dispatch()` at all, so the dispatch-level `if core_activity['type']
   == 'PollVote':` / `'ChooseAnswer':` checks at 1336 and 1340 are reached
   (every other dispatch check above them falls through to them) but their
   bodies, the calls to the two delegates from the dispatcher itself, never
   run. This means: `process_poll_vote` is fully covered as a function (see
   its own 100% row below); `process_question_answer` is not (23/24
   statements, 2 missing arcs — see gap 5 below). But neither delegate's
   own **dispatch site** is covered. A real, explained scope gap — this
   sub-project chose to test those two delegates as units rather than
   through the dispatcher a second time, which is defensible (Task 5 already
   proves the dispatch-and-delegate wiring pattern for the other two arms)
   but is not the same claim as "the four vote arms are covered."
4. **`process_downvote`, 8 of 18 statements and 6 arcs (lines 2418, 2421,
   2422, 2426-2429, 2433).** `process_downvote` is exercised by exactly one
   test that calls it as a function (`test_a_downvote_blocked_by_the_vote_quota_logs_ignored`,
   written specifically to pin the logging asymmetry against `process_upvote`
   — see D57 below) plus the dispatch-arm test above, which monkeypatches
   `process_downvote` itself and therefore never runs its body at all. Task
   5's own brief scoped the guard-drop mutation campaign to `process_upvote`
   only, treating the structurally identical `process_downvote` guards as
   out of scope for that step. The result, measured rather than assumed: the
   dict-shaped-`ap_id` unwrap (2418), the "liked object not found" refusal
   (2421-2422), the entire successful-downvote body (2426-2429: the vote
   itself, its success log, and the conditional announce), and the
   outer-guard refusal log (2433, `can_downvote`/`instance_banned` false) are
   all untested. None of this is a defect in `process_downvote` — every line
   it shares in shape with the fully-covered `process_upvote` (same
   guard structure, same delegate calls) is read, by inspection, as
   behaving identically — but it is a real, sizeable, and previously
   unstated coverage gap in this sub-project's own scope, and is recorded
   here rather than left implicit in a "18/18" that was never measured.
5. **`process_question_answer`, line 2468 and its branch (2467→2468), and
   branch 2493→(function exit).** Two independent, narrow gaps. First: no
   `process_question_answer` test passes a dict-shaped `ap_id` (`{'id':
   ...}`) the way `test_upvote_unwraps_a_dict_object_with_an_id_key` and
   `test_poll_vote_unwraps_a_dict_object_with_an_id_key` do for their own
   functions — Task 6 wrote that variant for `process_poll_vote` but not for
   `process_question_answer`, an asymmetry in test-matrix coverage between
   two sibling delegates, not in the delegates' own code (the unwrap is
   identical in all three functions). Second: every `process_question_answer`
   success test in this suite passes `announced=False`, so the `if not
   announced:` guard at line 2493 always takes its True arm
   (`announce_activity_to_followers` is called); the False arm — an
   announced `ChooseAnswer`, which skips the re-announce — is never
   exercised, unlike the equivalent guard in `process_upvote` (2407-2408,
   covered by `test_upvote_announced_reads_the_nested_object_and_does_not_re_announce`)
   and `process_poll_vote` (2455-2456, covered by
   `test_poll_vote_announced_reads_the_nested_object_and_choice_text`). Both
   gaps are narrow, explained, and symmetric with gap 3 above: this
   sub-project's per-delegate test matrices are not uniform across the four
   vote arms.
6. **Move, branch 1578→1590 (the whole Move body skipped) and branch
   1586→1588 (the announce-to-followers guard's False arm).** No test in
   `tests/test_inbox_dispatch_misc.py` constructs a Move where `origin_community`,
   `target_community` and `post` are not all three truthy by the time line
   1578 is reached — every test either finds the post locally or resolves it
   remotely via a doubled `resolve_remote_post_from_search` that always
   succeeds, so the case where the resolution still fails (or either
   community is unfound) and the entire Move body is silently skipped is
   unexercised. Separately, `_seed_move_scenario`'s own docstring records
   that `origin_community.is_local()` is `True` for every seeded scenario
   (`make_community()` never sets a remote `ap_id`), so line 1586's `if
   origin_community.is_local():` always takes its True arm — the case of a
   Move *originating* from a remote community, which does not need to
   announce to local followers, is never tested. Both are real, narrow scope
   gaps in the test matrix, not defects in the Move handler.

`process_upvote`, `process_poll_vote`, Flag, QuoteRequest and the
except/finally are all 100% statement and branch on their declared spans —
no gap to explain for those five.

### 2. New defects — D49-D63

None fixed, per this sub-project's report-only remit. Each was surfaced by
an implementer during Tasks 1-7 and is re-verified against source here, at
the point the row is written, per this document's standing rule. Severity is
argued from what the tests actually executed, not from reading, wherever a
test exists — several rows below say explicitly which half of the claim is
measured and which is reasoned.

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| D49 | `process_inbox_request` preamble | `activity['actor']` may be a dict (`if isinstance(actor_id, dict): actor_id = actor_id['id']`, routes.py:856-857) — a dict with no `'id'` key raises `KeyError: 'id'` immediately, before `find_actor_or_create_cached` is ever called and before any `log_incoming_ap` call is reachable. An uncaught 500-shaped failure for production's DEBUG branch, not a logged refusal, and no `ActivityPubLog` row is written. | medium — peer-triggerable, unhandled exception instead of a graceful refusal | **measured**: `pytest.raises(KeyError, match='id')` in `tests/test_inbox_dispatch_preamble.py` (Task 3) |
| D50 | `process_inbox_request` preamble | `'type' in request_json['object']` at routes.py:878 is a membership test on whatever `request_json['object']` is, not a type test — for a Community (Group) actor sending `Update` with a plain string object containing the substring `"type"`, the membership test passes and the very next line, `request_json['object']['type']` at routes.py:879, indexes a `str` with a `str` and raises `TypeError: string indices must be integers, not 'str'`, uncaught, no log row. The same shape this document's closing section ("A guard must be tested on the domain it claims to reject") already generalises from D13 and D30: **`KEY not in entry` is a call into `entry`, so a membership test is never a type test.** | medium — peer-triggerable, unhandled exception | **measured**: `pytest.raises(TypeError, match='string indices must be integers')` in `tests/test_inbox_dispatch_preamble.py` (Task 3) |
| D51 | `process_inbox_request`, Announce/OrderedCollection unwrap | `request_json['object']['orderedItems']` (routes.py:909, not 911 — see the documentation correction in section 4) is read with no guard. An `OrderedCollection` object with no `orderedItems` key raises `KeyError: 'orderedItems'`, uncaught, inside `process_inbox_request`'s own try block, propagating through routes.py:1885's `except Exception: session.rollback(); raise` and out of the dispatcher. No `ActivityPubLog` row. | medium — peer-triggerable, unhandled exception | **measured**: `pytest.raises(KeyError, match='orderedItems')` in `tests/test_inbox_dispatch_announce.py` (Task 4) |
| D52 | `process_inbox_request`, Announce inner-object walk | `request_json['object']['actor']` (routes.py:915) is read with no guard when the Announce's inner object is a dict. An inner object with no `'actor'` key raises `KeyError: 'actor'`, same uncaught propagation as D51, no log row. | medium — peer-triggerable, unhandled exception | **measured**: `pytest.raises(KeyError, match='actor')` in `tests/test_inbox_dispatch_announce.py` (Task 4) |
| D53 | `process_inbox_request` preamble | routes.py:859-860 — `if actor_id and actor_id.startswith('https://s.rimu.geek.nz'): pass  # just here to set breakpoints on, during testing. remove before commit`. A shipped debugging hook, its own comment asking for its removal, left in production code. No behaviour to trigger (the arm is inert) and no test exercises it — see gap 1 in section 1 above for why that is the right call, not an oversight. | cosmetic | reading; this sub-project's own coverage measurement confirms the arm's True branch is never taken by any test, consistent with there being nothing to test |
| D54 | `process_inbox_request`, Announce list/OrderedCollection unwrap | routes.py:901-912 recurses into `process_inbox_request` itself once per element of an Announced list or `orderedItems` array, with no bound on the array's length and no bound on recursion depth (each element re-enters the full preamble, including this same Announce-unwrap block, so a maliciously nested structure recurses rather than merely looping). Task 4 proved the recursion is genuine — not a doubled dispatcher — by mutating line 902 to `request_json['object'][:1]` and watching `test_an_announce_of_a_list_processes_every_element` fail (`assert 1 == 2`). No test sends more than two elements or any nesting, so the resource-exhaustion consequence (an oversized list driving unbounded work per inbox POST, or sufficiently nested `orderedItems` driving Python's own recursion limit) is not demonstrated end to end, only that the recursive mechanism itself is real and unbounded by inspection of the loop. | low-medium — availability; peer-triggerable in principle, but the size/depth needed to matter was not measured | **measured**: the recursion mechanism (mutation-kill, Task 4); **reasoned, not measured**: that an attacker-sized input actually exhausts a resource |
| D55 | `process_poll_vote` | `request_json['choice_text']` (routes.py:2440, non-announced path) is read with no guard, before `Post.get_by_ap_id` is even called. A `PollVote` with no `choice_text` key raises `KeyError: 'choice_text'` regardless of whether the target post exists. Same unguarded-read shape as D49/D51/D52/D56. | medium — peer-triggerable, unhandled exception | **measured**: `pytest.raises(KeyError, match='choice_text')` in `tests/test_inbox_dispatch_votes.py` (Task 6) |
| D56 | `process_inbox_request`, QuoteRequest arm | `core_activity['instrument']['id']` (routes.py:1882) is read with no guard. A `QuoteRequest` with no `'instrument'` key raises `KeyError: 'instrument'` before `process_quote_boost` is ever called and before the SUCCESS log at routes.py:1884 is reachable; no `ActivityPubLog` row. Same shape as D49/D50/D51/D52/D55 — this dispatcher's preamble and several of its arms read peer-supplied keys unguarded while others (e.g. the `'key' in dict'` idiom used correctly elsewhere in this codebase) do not. | medium — peer-triggerable, unhandled exception | **measured**: `pytest.raises(KeyError, match='instrument')` in `tests/test_inbox_dispatch_misc.py` (Task 7) |
| D57 | `process_upvote` / `process_downvote` | The two functions' otherwise-identical guard shapes log asymmetrically on refusal. `process_upvote`'s inner `if` (routes.py:2403-2408) has no `else` at all — its only `else` (2409-2410) belongs to the *outer* `if`, so an upvote blocked by the *inner* conjunction (a non-`Post`/`PostReply` target, a block between voter and author, or an exceeded vote quota) logs nothing: `ActivityPubLog.query.count() == 0` even with logging enabled. `process_downvote`'s structurally identical inner `if` (2424-2429) has its own `else` (2430-2431) logging `'Cannot downvote this'` / `APLOG_IGNORED`. The same input (an over-quota voter) produces zero log rows through one delegate and one `ignored` row through the other. An operator monitoring `ActivityPubLog` for blocked votes would see every blocked downvote and miss every inner-guard-blocked upvote. | low — diagnostic/operational asymmetry, no security or data-integrity impact | **measured**: `test_an_upvote_blocked_by_the_vote_quota_logs_nothing` (asserts zero rows) and `test_a_downvote_blocked_by_the_vote_quota_logs_ignored` (asserts one `ignored` row) against the same quota-exceeded input, `tests/test_inbox_dispatch_votes.py` (Task 5) |
| D58 | `process_inbox_request`, Move arm | The Move guard at routes.py:1579 (`user.id == post.user_id or origin_community.is_moderator(user) or (origin_community.instance_id == user.instance_id and origin_community.is_instance_admin(user))`) has no `else`; when every alternative is false, control falls straight through to the next activity-type check (`'Block'`, routes.py:1590) with nothing logged and the post left unmoved. The refused Move produces **zero** `ActivityPubLog` rows, unlike essentially every other refusal path in this function (compare D49-D56, D63, and the ordinary Flag/Update/Add/Remove refusals, all of which log something). The security outcome is correct — the post does not move — but a hostile peer repeatedly attempting an unauthorized Move leaves no operational trace at all. | low — silent refusal with no diagnostic trace; no security or data-integrity impact, since the post correctly does not move | **measured**: `test_a_move_by_an_unrelated_user_does_nothing` asserts `ActivityPubLog.query.count() == 0` with logging enabled, `tests/test_inbox_dispatch_misc.py` (Task 7) |
| D59 | `find_actor_or_create_cached` / `_find_actor_id_cached` (`app/activitypub/util.py:313-360`), every call site | **The Redis ID-cache fast path bypasses the banned-actor filter for up to ten minutes, and this generalises beyond the inbox dispatcher.** `_find_actor_id_cached` is `@cache.memoize(timeout=600)` (util.py:313) and caches only `(id, class_name)` (util.py:320) — never the model, never a banned flag. On a cache hit, `find_actor_or_create_cached` re-fetches the row with a bare `db.session.get(User\|Community\|Feed, actor_id)` (util.py:356-360), which applies **no banned filter of any kind**. A user banned within the ten-minute window after being resolved while in good standing therefore resolves as valid to any caller that does not perform its own explicit re-check. Two call sites in this codebase already know this and guard against it explicitly — `process_inbox_request`'s own Announce inner-actor walk (`if user.banned:`, routes.py:917-919, the site this sub-project's Task 4 exercised by monkeypatching `_find_actor_id_cached` to simulate a stale hit under `NullCache`) and `process_announce_of_uri`'s own backstop (`if announcer.banned:`, util.py:3899, whose own comment already names this exact mechanism: "this branch only fires for a `_find_actor_id_cached()` entry cached before the actor was banned, which bypasses that upstream check"). **Every other caller does not.** `grep -rn 'find_actor_or_create_cached(' app/` finds 27 call sites; besides the two guarded ones above, this includes `process_inbox_request`'s own preamble lookups (routes.py:862, 864, 866, 871 — none re-check `.banned` on the resolved actor), the gate's own actor resolution in `shared_inbox`/`replay_inbox_request` (routes.py:703, 817), and at least eleven further sites across the moderation arms (Follow, Add/Remove Moderator, Move's origin/target, Block, private messages) that this sub-project did not audit individually. **Reach beyond the two confirmed sites is a reading-level claim, not a measured one** — no test in this sub-project demonstrated a stale-cache hit reaching any call site other than routes.py:915-919 (which Task 4 already had to simulate via monkeypatch, since this suite's `NullCache` config never actually caches). | medium-high — security-relevant, a caching side-channel that can admit a banned actor for up to ten minutes at any unguarded call site; **confirmed reachable and simulated at one site (routes.py:915-919), reasoned but not demonstrated at the other ~25** | **measured** at one call site: Task 4's `test_an_announce_whose_inner_actor_is_banned_is_refused` (monkeypatches `_find_actor_id_cached` to simulate a warm cache entry, then bans the user, and confirms the 917-919 backstop fires); **reading-only** for every other call site's exposure |
| D60 | `process_inbox_request`, the dispatcher's own `session` local vs. `db.session` | **The inline (DEBUG) and queued (Celery) dispatch paths run under different session arrangements, and this is observable only for part of the function.** `patch_db_session` (`app/utils.py:3663-3673`) only replaces `db.session` when `has_request_context()` is false. Under a direct call (`dispatch()`, Task 1) there is no request context, so patching occurs: `db.session` is reassigned to a `SessionWrapper(task_session)` (`app/utils.py:3678-3692`) whose `__getattr__` proxies every attribute access through to the dispatcher's `session` local — not the identical object, but behaviourally equivalent for attribute access (reads and method calls), which is the precision this row owes the earlier, looser phrasing Task 1 deferred to this pass. Under a real signed HTTP request (Task 8's three seam tests), `has_request_context()` is True, so patching does **not** occur: the dispatcher's `session` local (from `get_task_session()`) stays an independent `Session(bind=db.engine)` while `db.session` remains the request-scoped session, genuinely unrelated to it. **Both halves matter, and neither should be read alone.** Task 8 established, empirically, that this divergence is **NOT observable in its three seam tests**, because `find_actor_or_create_cached` and everything beneath it (`find_actor_or_create`, `find_actor_by_url`, `find_remote_actor`) always resolve through `db.session` — never through the dispatcher's `session` local — so both the gate's actor resolution and the dispatcher's re-resolution land on the same session object regardless of which arrangement is in effect. But `process_inbox_request` itself contains lookups that DO use the `session` local directly: `session.query(CommunityBan)` (routes.py:950), `session.query(ChatMessage)` (routes.py:1319, 1739), and a third instance inside `process_chat` (`app/activitypub/routes.py:2502-2600`, called from `process_inbox_request` at routes.py:1203 and :1225 with `session` passed through as a parameter) — `session.query(ChatMessage)` at routes.py:2558. None of these four call sites is exercised by any test in this sub-project — Flag, Move, QuoteRequest and the vote arms never reach them, and Task 8's three seam tests are Like/Announce-shaped, not Block/ChatMessage-shaped. So the divergence Task 1 flagged is real and would be live for those lookups under the request-context path, but no test in this repository (as of this sub-project) exercises a request-context call that reaches any of them, so the claim that it actually diverges observably is **reasoned from source, not measured**. These four are the call sites this row's own citations confirm; `process_inbox_request`'s full body (839-1889) contains roughly 44 `session.query(...)` call sites in total, spanning roughly a dozen additional models (`CommunityMember`, `CommunityJoinRequest`, `FeedMember`, `User`, `UserFollower`, `FeedJoinRequest`, and others) beyond the four cited here — that the remaining ~40 sites are similarly live for the same session-arrangement divergence is a reading-level claim, not one this sub-project measured or audited site-by-site. | informational — a genuine architectural asymmetry between two production paths, confirmed not to matter for the paths this sub-project tested; **confirmed** live for the four cited lookups (routes.py:950, :1319, :1739, :2558), **reading-level, not measured**, for the remaining roughly 40 of the function's roughly 44 total `session.query(...)` call sites | **measured** (does not diverge): Task 8's three seam tests, for `find_actor_or_create_cached` only. **Reasoned, not measured** (would diverge): the `CommunityBan`/`ChatMessage` lookups (including the one inside `process_chat`), none of which any test in this sub-project reaches under a request context |
| D61 | `process_inbox_request` preamble | `actor and isinstance(actor, User)` (routes.py:872) and `actor and isinstance(actor, Community)` (routes.py:874) — the `actor and` half of both compound guards is an **equivalent mutant (dead code)**. `find_actor_or_create_cached`'s return type is `User \| Community \| Feed \| None`; none of the three model classes overrides `__bool__`/`__len__`, so every value `actor` can take is either `None` (already falsy, and `isinstance(None, ...)` is already `False`) or a real ORM instance (always truthy). `actor and X` and bare `X` therefore evaluate identically for every reachable input. Confirmed empirically, not by argument alone: dropping either `actor and` clause and re-running the full 14-test (at the time) `tests/test_inbox_dispatch_preamble.py` suite left it fully green. | informational — dead code, no behaviour to change or test | **measured**: both mutants run against the full suite, both survived (Task 3) |
| D62 | `process_upvote` | `isinstance(liked, (Post, PostReply))` (routes.py:2403) is an **equivalent mutant (dead code)**. `find_liked_object` (`app/activitypub/util.py:2024-2046`) is typed `Union[Post, PostReply, None]`; it has four return statements (`return None` at 2039 and 2046, `return post` at 2040, `return db.session.get(PostReply, obj_id)` at 2042), and none of the four can produce any type outside that union; `process_upvote`'s own early return two lines above (2399-2401) already handles the `None` case, so by the time this line runs `liked` can only ever be a `Post` or `PostReply` — the check can never observe `False`. Confirmed empirically: dropping it and re-running the full 14-test `tests/test_inbox_dispatch_votes.py` suite (as it stood at the time) left all 14 green. | informational — dead code, no behaviour to change or test | **measured**: the mutant run against the full suite, survived (Task 5) |
| D63 | `process_inbox_request` preamble | The Announce/Accept/Reject actor-not-found refusal (routes.py:868) always logs `APLOG_ANNOUNCE` (`app/constants.py:132`), even when the activity that triggered it was an `Accept` or a `Reject`, not an `Announce`. An operator reading `ActivityPubLog` for a refused `Accept`/`Reject` sees it mislabelled as the wrong activity kind. | cosmetic | reading; surfaced while writing Task 2's outcome table, not independently re-run against a test, since no test in this sub-project distinguishes the three activity types at this specific log call |

### 3. A test gap distinct from a defect: `create_if_not_found=False`'s untested semantic effect

Not D-numbered, because nothing here is wrong with the code — this is a gap
in what this sub-project's tests can claim, recorded per the same standard
D21's qualification and sub-project 2's Step 3 already used. Task 2 dropped
`create_if_not_found=False` from routes.py:862 (the community lookup in the
Announce/Accept/Reject preamble) twice. The first run "killed" four tests,
but every failure was `respx.models.AllMockedAssertionError` — an
infrastructure fact (removing the guard causes an attempted fetch) established
before any of this file's own assertions ran, per the Task 2 standing rule
that such a kill is not evidence about the guard's effect on *which actor
gets resolved*. Re-run with the fetch served successfully (a mocked GET
returning the pre-existing row's own document), **all tests passed
unchanged**: `actor_json_to_model`'s dedup query finds the same row that was
already on file, `create_actor_from_remote` returns it, and the
`community_only` discard applies identically with or without the guard. So
**this sub-project's own tests do not discriminate `create_if_not_found=False`'s
semantic effect**, only its infrastructure-visible side effect (an attempted
fetch). Task 2's report names the one scenario that would discriminate it —
a document that deserializes to a *different* model class than the row
already on file at that URL (e.g. a `Group` document served at a URL this
suite only ever seeded as a `User`/`Feed`) — and explicitly leaves
constructing it to whoever next touches this guard.

### 4. Documentation corrections, in this sub-project's own record

Two errors the ledger flags in this sub-project's own planning and test
documentation, corrected here rather than by editing the test files
themselves (per the report-only constraint):

- **The `orderedItems` read is at routes.py:909, not :911.** Both this
  sub-project's plan/brief and (faithfully, since a test docstring is
  supposed to describe the code it exercises) `tests/test_inbox_dispatch_announce.py`'s
  own docstring cite line 911. Read against source (reproduced in section 1
  of this entry), the read is `for obj in request_json['object']['orderedItems']:`
  at line 909; line 911 is the loop body's `fake_activity['object'] = obj`
  two lines further down. D51 above cites the corrected line number.
- **Task 4's feed-path test overclaims what it proves about `user` being
  "cleared."** The test's docstring says it proves `user` was explicitly set
  to `None` at routes.py:924 (the Announce arm's `else: user = None`, taken
  when the outer actor resolved as a Feed). It does prove the walk was
  *skipped* — `process_upvote` is monkeypatched and receives `user_arg is
  None` while a banned inner actor that would have tripped the 917-919
  refusal had the walk run is present in the fixture, which only holds if
  the walk never ran. But routes.py:858 already runs `feed = community =
  user = None` on **every** call, before any branch — so `user` is `None`
  going into the Feed arm regardless of whether line 924 executes at all.
  Deleting routes.py:923-924 outright would leave this test green. The
  walk-skip claim is proven; the claim that line 924 specifically is what
  cleared `user` is not, and is withdrawn here.

### 5. Converting D21, D24 and D35's reachability claims — upgrade only what the tests support

Sub-project 4 established the standard paragraph for this ("upgrade only
what the tests support... the campaign's most-repeated correction"), and
this sub-project has to write it again for the same mechanical reason: it
executes more of the dispatcher than sub-project 4 did, but still not the
functions D21, D24 and D35 actually describe.

`tests/test_inbox_dispatch_preamble.py`'s module docstring states, for
Task 8's three seam tests, what they license — quoted here verbatim except
for one correction the review caught (the source said "the vote arms",
plural; only `process_upvote` was ever driven through the gate,
`process_downvote` never was — rendered singular below):

> What these three tests LICENSE: that a real signed peer reaches the
> preamble (routes.py:839-931), the Announce unwrap (routes.py:899's
> process_announce_of_uri call), and **the upvote arm** (routes.py:1329's
> process_upvote call), for exactly three shapes -- a Like from a known
> User, an Announce of a plain-string object from a known User, and (for the
> third test) the same Like shape again, used to pin that the actor object
> the gate verified the signature against and the actor object the arm
> receives are the same row.

Every other test in this sub-project's four files calls `process_inbox_request`
directly, through the `dispatch()` helper — none of them goes through
`shared_inbox` or any real HTTP request. So "through the gate" applies to
exactly those three tests, and only those three.

**Do any of the three touch D21, D24 or D35's own functions?** No, for the
same mechanical reason sub-project 4 gave for its own tests, one layer
deeper. `test_a_signed_like_reaches_the_upvote_arm_through_the_gate` and
`test_the_actor_the_gate_verified_is_the_actor_the_arm_receives` both
monkeypatch `process_upvote` — a function D21/D24/D35 do not describe at
all. `test_a_signed_announce_reaches_the_unwrap_through_the_gate` monkeypatches
`process_announce_of_uri` itself and asserts only that it was reached with
`community is None`; it never runs `process_announce_of_uri`'s real body,
which is where D21's `netloc` comparison and D35's reply-creation defect
actually live. No test anywhere in this sub-project executes
`resolve_remote_post`, `verify_object_from_source`, `create_resolved_object`
or `resolve_remote_post_from_search` — the functions D21, D24 and D35
respectively describe — for real; Task 4's Move test doubles
`resolve_remote_post_from_search` outright (see the report's construction
note), which is D24's own call site.

**Therefore: D21, D24 and D35 are unchanged by this sub-project.** They
remain exactly as reading- or probe-backed as sub-projects 2b, 3 and 4 left
them. This sub-project's genuine, newly-measured contribution is one layer
higher: that a real signed peer, through the real gate, reaches
`process_inbox_request`'s own preamble and its Announce-unwrap and
upvote-dispatch call sites — a fact that did not exist as tested behaviour
before Task 8, and that sub-project 4's own tests (which monkeypatch
`process_inbox_request` itself away) could not have shown. Claiming more
than that — that this sub-project's tests say anything new about D21, D24
or D35's own behaviour — would repeat the exact error this campaign keeps
correcting.

### 6. The allocation ledger, updated

D1–D20 sub-project 2a, D21–D24 sub-project 2b, D25–D29 sub-project 2c,
D30–D33 sub-project 2c's whole-branch review, D34–D40 sub-project 3, D41–D46
sub-project 4, D47 sub-project 4's whole-branch review, D48 the follow-on
audit of `instance_allowed`/`instance_banned`. **D49–D63 this sub-project**
(section 2 above): D49-D52, D55-D56 the unguarded peer-supplied reads; D53
the shipped breakpoint hook; D54 the unbounded Announce recursion; D57 the
upvote/downvote logging asymmetry; D58 Move's silent no-op; D59 the
Redis ID-cache banned-actor bypass, generalised beyond this call site; D60
the inline/queued session-arrangement divergence; D61-D62 the two equivalent
mutants; D63 the `APLOG_ANNOUNCE` mislabelling.

## Sub-project 5b: the Follow, Accept and Reject arms of the membership handshake

`docs/superpowers/specs/2026-08-30-coverage-inbox-membership-5b-design.md`, on
branch `blentz`. Eight test-writing tasks plus this report-only task covered
`process_inbox_request`'s three membership-handshake arms directly downstream
of the preamble 5a covered: Follow (routes.py:935-1073, all three targets --
Community, Feed, User), Accept (1075-1148) and Reject (1150-1191). 43 tests
across two files: `tests/test_inbox_dispatch_follow.py` (18) and
`tests/test_inbox_dispatch_accept_reject.py` (25, including Task 5's and
Task 7's fix-verification tests). This sub-project carried an explicit,
bounded exception to the campaign's report-don't-fix rule: **Tasks 5 and 7
were separately authorised by the project owner to fix exactly two defects
each found while writing its own tests** (recorded as D76 and D77 below),
while every other defect this sub-project found -- in Tasks 1-4, 6, 8 and
this task -- was registered, not fixed. `git diff --stat app/` is empty for
every task except 5 and 7, each confined to the one function this row names.

### 1. Whole-unit coverage, and every gap explained

Measured against this sub-project's own two files, per this task's brief:

```bash
./run_tests.sh tests/test_inbox_dispatch_follow.py tests/test_inbox_dispatch_accept_reject.py \
  -q --cov=app.activitypub.routes --cov-report=json
```

43 passed. This is deliberately narrower than 5a's own whole-branch command
(which ran all four of that sub-project's files together) -- the brief for
this task named exactly these two files, so the blended module-level
`percent_covered` reported here (15.78%, 333/1814 statements by
`percent_covered`'s own statement count, 94/892 branches) is **not**
comparable to 5a's 18.35% figure without accounting for that difference; it
reflects only what these two files exercise, not a regression. The number
that matters for this task is the three spans' own figures, intersected from
`coverage.json`'s `executed_lines`/`missing_lines`/`executed_branches`/
`missing_branches` against `935-1073`, `1075-1148` and `1150-1191`, the same
method 5a and earlier sub-projects used:

| span | statements | branches |
|---|---|---|
| Follow (935-1073) | 80/80 executed | 32/34 executed, 2 missing arcs |
| Accept (1075-1148) | 60/60 executed | 29/32 executed, 3 missing arcs |
| Reject (1150-1191) | 36/36 executed | 22/24 executed, 2 missing arcs |

**Every statement in all three spans is executed.** Zero missing lines --
this sub-project's eight test-writing tasks between them drove every branch
of the Community/Feed/User dispatch in Follow, every one of Accept's three
target branches plus its a.gup.pe string-object and IntegrityError paths,
and all three of Reject's target branches. The only gaps are the seven
missing branch arcs below, each explained rather than left as a remainder.

**Documentation correction, in the same spirit as this document's recurring
"a hand-carried figure was wrong" theme:** the brief and this sub-project's
own plan state Reject's span as 35 statements. Measured directly against
`coverage.json`, it is **36** -- one more than declared. Re-counted by hand
against source: the span (1150-1191) contains exactly 36 executable
statement lines (blank line 1191 is not one of them). Not a defect, and it
does not change the "every statement executed" claim above; recorded here so
a future reader who re-derives the count is not surprised to find 175 was
one short of what this task actually measured (176 = 80+60+36).

**The seven missing branch arcs, in full:**

1. **Follow, arc `[1018, 1072]` -- the User target's `elif isinstance(target,
   User):` False-skip.** This is an **equivalent mutant / dead branch**, by
   the same type-narrowing argument 5a's D61/D62 established for the
   preamble's `actor and isinstance(...)` guards and `process_upvote`'s
   `isinstance(liked, (Post, PostReply))` check. `target` comes from
   `find_actor_or_create_cached(target_ap_id)` (routes.py:938), typed
   `User | Community | Feed | None`; the `not target` case already returned
   at :939-941, and both the Community branch (:942, returns at :981) and
   the Feed branch (:983, returns at :1017) return from inside their own
   bodies before control can ever reach line 1018. So by the time line 1018
   is evaluated, `target` is neither `None`, `Community` nor `Feed` --
   it must be `User`, and the `elif`'s False arm can never fire. No test
   exercises it because no input can.
2. **Follow, arc `[1034, 1036]` -- the `if not local_user.ap_followers_url:`
   False-skip (routes.py:1034-1035).** A real, narrow test-matrix gap, not a
   defect: every User-target test in this file's fixtures leaves
   `ap_followers_url` unset on the local target, so the guard's True arm
   (backfilling it) fires every time; no test seeds a local user who
   already has `ap_followers_url` populated before a remote Follow arrives,
   which is the only way to exercise the False arm.
3. **Accept, arc `[1084, 1091]` -- the a.gup.pe string-object path's
   `if join_request:` False-skip (routes.py:1084-1085).** A real, narrow
   test-matrix gap. Task 5's two string-object tests
   (`test_an_agupe_string_accept_admits_the_join_requests_user`,
   `test_an_agupe_numeric_style_accept_retries_by_primary_key`) both embed a
   real, existing `CommunityJoinRequest`'s own `uuid` or `id` in the
   string, so `join_request` is always found by one of the two lookups.
   Neither test tries a string whose trailing segment matches no join
   request at all -- the case the code's own `if not requestor_user:`
   check at :1091 exists to handle gracefully (log FAILURE, return) is
   never driven for this specific (string-object) entry to that check.
4. **Accept, arc `[1086, 1091]` -- the `elif core_activity['object']['type']
   == 'Follow':` False-skip (routes.py:1086-1090), for a dict-shaped
   object.** A real, narrow test-matrix gap, and the Accept-side mirror of
   something this sub-project DID test on the Reject side. Every Accept
   test in this suite sends `object` as either the a.gup.pe string form or
   `_follow_object(...)`, whose `'type'` is always `'Follow'` --
   Task 6's own outcome-table comment names the "object dict, type !=
   'Follow'" case explicitly (`requestor_user` stays `None`, falls to
   :1091's FAILURE) but no test constructs it. Contrast Task 8's Reject-side
   `test_a_reject_of_a_non_follow_object_is_silently_ignored`, which does
   send a `{'type': 'Undo', ...}` object -- the same shape exists as an
   untested table row on the Accept side and a tested one on the Reject
   side.
5. **Reject, arc `[1150, 1193]` -- the top-level `if core_activity['type']
   == 'Reject':` False-skip, falling through to the Create/Update check at
   :1193.** Legitimately out of this sub-project's scope, not a gap in
   Reject's own coverage. Within this two-file suite, the ONLY way to reach
   line 1150 at all is a genuine `Reject`-type activity: every Follow test
   returns at :1072 and every Accept test returns at :1147, both strictly
   before line 1150 is ever reached, so no test in either file could ever
   present a non-`Reject` type at that check. Driving the False arm needs a
   `Create`/`Update`/other-type activity, which is a different sub-project's
   scope entirely (this function's dispatch chain extends far past line
   1191).
6. **Accept, arc `[1130, 1147]` -- the `elif user:` False-skip
   (routes.py:1130-1146).** An **equivalent mutant / dead branch**, the same
   type-narrowing shape as gap 1 above, one level up: by the time Accept's
   own dispatch reaches line 1130, both `if community:` (:1095) and
   `elif feed:` (:1118) have already evaluated False. The preamble's own
   actor-not-found refusal (routes.py:868, 5a's D63 row) already returns
   before this code if the Accept's outer actor resolved to none of
   community/feed/user at all -- so reaching line 1130 guarantees the actor
   resolved to exactly one of the three, and having ruled out the first two,
   `user` must be truthy. No input can make this arc's False direction fire.
7. **Reject, arc `[1179, 1190]` -- the `elif user:` False-skip
   (routes.py:1179-1189).** The same equivalent-mutant argument as gap 6,
   applied to Reject's identically-shaped `if community: elif feed: elif
   user:` dispatch.

None of the four dead-branch gaps (1, 6, 7, and D61/D62's own two from 5a)
were run as an explicit mutation-and-survive check the way 5a did for its
two -- this sub-project's own test-writing tasks did not target these three
arcs specifically, and this report-only task did not add mutation evidence
beyond the type-narrowing argument from source, which is why they are
presented as reading-level reasoning rather than a measured mutant survival,
unlike 5a's D61/D62.

### 2. New defects -- D64-D75

None fixed by this task, per its report-only remit. D76 and D77 (section 3
below) are the two exceptions this sub-project's own authorisation carved
out, and both were fixed by the tasks that found them, not by this one.
Severity is argued from what the tests actually executed wherever a test
exists; several rows below say explicitly which half of the claim is
measured and which is reasoned from source.

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| D64 | `process_inbox_request`, Follow/User auto-accept (routes.py:1030-1071) | The `UserFollower` row (`session.add`/`session.commit()`, :1036-1037) and its `Notification` (`db.session.add`/`db.session.commit()`, :1067-1069) are written through two different session objects inside one logical write -- the concrete instance, one line deeper, of 5a's D60 session-arrangement divergence. Under a direct `dispatch()` call (this sub-project's own harness), `patch_db_session` reassigns `db.session` to a wrapper proxying to the same task-local `session`, so both writes land on what is behaviourally the same object; under a real request-context dispatch (the gate's own path), `patch_db_session` does not fire, and the two are genuinely independent sessions. | informational -- confirmed equivalent under the harness this sub-project tests through; the request-context divergence is D60's own claim, not independently re-demonstrated here | **measured**: Task 4's `test_the_follower_row_and_its_notification_both_land_under_a_direct_dispatch` (renamed 2026-08-30, fix-wave review: its prior name, `test_..._are_written_through_different_sessions`, claimed the divergence the test's own docstring says it cannot show) confirms both rows land under direct dispatch, and its own docstring states plainly it cannot exhibit D60's request-context divergence; **reading-level**: that the divergence is real under a request-context path (D60, 5a) |
| D65 | `process_inbox_request`, Follow/Feed target (routes.py:989-999) | The Feed reject path sends a `Reject` over the wire but calls `log_incoming_ap` nowhere on this path, unlike Community's two reject reasons (:945-953), which both log `APLOG_FAILURE` before sending. A rejected Feed-follow attempt leaves no `ActivityPubLog` row at all. | low -- diagnostic/audit-trail asymmetry; the reject itself is correct, only its record is missing | **measured**: `test_a_follow_of_a_non_public_feed_is_rejected_without_any_log` asserts `ActivityPubLog.query.count() == 0` with `LOG_ACTIVITYPUB_TO_DB` enabled (Task 3) |
| D66 | `process_inbox_request`, Follow, all three targets' already-related paths (Community :964-965, Feed :1001, User :1030) | Each writes nothing, sends nothing, and logs nothing when the relationship already exists (an existing `CommunityMember`, an already-`SUBSCRIPTION_MEMBER` `FeedMember`, an existing `UserFollower`). A peer that re-sends a Follow it already holds leaves zero trace on either the DB audit log or the wire, uniformly across all three targets. | low -- same shape as 5a's D58 (silent no-op, no security/data-integrity impact since the correct outcome, no duplicate relationship, already holds) | **measured**, three distinct tests: Task 2's `test_a_follow_from_an_existing_member_writes_nothing_and_stays_silent`, Task 3's `test_a_follow_from_an_existing_feed_member_writes_nothing_and_stays_silent`, Task 4's `test_a_follow_from_an_existing_inward_follower_writes_nothing_and_stays_silent` |
| D67 | `process_inbox_request`, Follow/User target (routes.py:1033) | `is_accepted=auto_accept if auto_accept else None` can only ever write `True` or `None` on this path -- correct and confirmed by the parametrised test. `UserFollower.is_accepted`'s own column comment (`app/models.py:3533`) documents `False` ("Rejected") as a third, meaningful state that this specific write path can never produce. **Corrected 2026-08-30 (fix-wave review): the universal that follows -- "a `UserFollower` can only ever reach `False` through Reject's own write at routes.py:1186, never through Follow's own acceptance logic" -- is false and is struck.** `app/shared/user.py:239-248` (`follow_user`) is a second, ordinary write path: it initialises `is_accepted = False` and only reassigns it inside `if to_follow.is_local():`, so a local user following a REMOTE user writes `is_accepted=False` directly, with no Reject involved. Scoped correctly: `routes.py:1033` (the inward-follow write this row is actually about) can only ever produce `True` or `None`, which is what the test demonstrates; the false claim was the broader statement about every way a `UserFollower` row can reach `False`. | informational -- a dead state on this one write path (:1033), not a bug (nothing on this path is meant to write `False`); unrelated to the separate, ordinary `False` write in `follow_user` | **measured**: `test_a_follow_never_records_is_accepted_false`, parametrised over `manually_approves in [False, True]`, asserts `is_accepted != False` in both (Task 4), scoped to routes.py:1033 only |
| D68 | `process_inbox_request`, Reject (routes.py:1154, 1168, 1178, 1189) | All four of Reject's `log_incoming_ap` calls -- the unresolvable-actor failure and all three target branches' success logs -- pass `APLOG_ACCEPT`. Every `ActivityPubLog` row a Reject produces reads as an Accept. Same defect class as 5a's D63 (the preamble's Announce/Accept/Reject refusal always logging `APLOG_ANNOUNCE`), here spanning the whole arm rather than one preamble line. | cosmetic -- operator-facing mislabeling only. **Corrected 2026-08-30 (fix-wave review): the clause "the correct membership/follow-request deletion still happens on every branch" is false and is struck.** `routes.py:1180-1189`'s user branch queries the `UserFollowRequest` and never deletes it, unlike the community (:1161) and feed (:1172) branches, which both call `session.delete(join_request)`. See D79 below for the missing delete itself. | **measured** at one of the four sites: `test_a_reject_is_logged_as_an_accept` asserts `log.activity_type == APLOG_ACCEPT[1]` on the community branch's success path (:1168); **reading-level** for the other three sites (:1154, :1178, :1189), which reuse the identical `log_incoming_ap(id, APLOG_ACCEPT, ...)` call shape, confirmed by source inspection rather than one test per site (Task 8) |
| D69 | `process_inbox_request`, Reject (routes.py:1151) | When the Reject's inner `object['type']` is not `'Follow'`, the `if core_activity['object']['type'] == 'Follow':` check has no `else` -- control falls straight to the outer `return` at :1190 having done nothing. No log row, no error, no indication a Reject was even received. | low -- silent no-op, same shape as 5a's D58 (Move's missing-`else` fallthrough); no security or data-integrity impact | **measured**: `test_a_reject_of_a_non_follow_object_is_silently_ignored` sends `object={'type': 'Undo', ...}` and asserts `ActivityPubLog.query.count() == 0` with logging enabled (Task 8) |
| D70 | `process_inbox_request`, Accept (routes.py:1086) and Reject (routes.py:1151) | Both read `core_activity['object']['type']` with no guard that the key exists and no guard that `core_activity['object']` is even a dict. An object dict with no `'type'` key raises `KeyError`, uncaught, before any log row is written; an object that is neither a dict nor (for Accept only, via :1077) a string -- a bare list or int -- raises `TypeError` indexing it. **Added 2026-08-30 (fix-wave review): the concrete motivating case, not just an abstract example.** Reject's `:1151` read has no string-object escape hatch the way Accept's `:1077` does, so an a.gup.pe-shaped Reject carrying a bare string object -- the same peer software whose string-shaped Accepts Fix 1/D76 exists to admit -- raises `TypeError: string indices must be integers` on this line, uncaught, rather than being handled the way its Accept counterpart is. The same unguarded-peer-supplied-key shape this document already generalises from D13/D30 and 5a's D49-D52/D55/D56. | medium -- peer-triggerable, unhandled exception instead of a graceful refusal, consistent with the severity this document has given this shape throughout | **reading-level only** -- no test in this sub-project sends an Accept or Reject whose object lacks a `'type'` key or is neither a string nor a dict; every object in this suite is a full URL string (Accept's a.gup.pe path only) or a dict that always carries `'type'` |
| D71 | `process_inbox_request`, Accept/community branch (routes.py:1108) | `User.query.get(join_request.user_id).bot` reads through Flask-SQLAlchemy's legacy `Model.query`, bound to `db.session` -- not the function's own task-local `session` object every other query in this arm uses (`session.query(User).get(...)` at :1085, `session.query(CommunityJoinRequest)` at :1096, and five more in this arm alone). A second instance of 5a's D60 session-arrangement fact inside the very code path Task 4's D64 test already demonstrated it for. Also emits SQLAlchemy's `LegacyAPIWarning` on every call (`Query.get()` is deprecated in favour of `Session.get()` under SQLAlchemy 2.0). | informational/low -- a deprecation plus an architectural inconsistency; not demonstrated to produce an incorrect `bot` read, since the value is read-only here with no write race shown | **measured** that the warning fires, observed directly in this task's own coverage run output; **reading-level** that this constitutes a second, distinct session object under a request-context dispatch (extending D60/D64's reasoning, not independently re-demonstrated for this call site) |
| D72 | `process_inbox_request`, Accept/community branch (routes.py:1114) | **The `IntegrityError` handler is bound to the wrong exception class.** `routes.py:6` is `from psycopg2 import IntegrityError`, so `except IntegrityError:` at :1114 is bound to psycopg2's driver-level class, not `sqlalchemy.exc.IntegrityError`. SQLAlchemy wraps every DBAPI exception it catches from the underlying driver in its own class hierarchy before re-raising, and the ORM code this handler guards (`session.add(member)`/`session.commit()` at :1107/:1111, both against a SQLAlchemy `Session`) raises through that hierarchy, not psycopg2's. Verified in the running container (SQLAlchemy 2.0.52): `sqlalchemy.exc.IntegrityError`'s MRO is `IntegrityError -> DatabaseError -> DBAPIError -> StatementError -> SQLAlchemyError -> Exception`; `psycopg2.IntegrityError`'s MRO is `IntegrityError -> DatabaseError -> Error -> Exception`. The two hierarchies share only `Exception`, and `issubclass(sqlalchemy.exc.IntegrityError, psycopg2.IntegrityError)` is `False`. So a genuine concurrent-membership race -- two Accepts for the same join request landing close enough that both pass `if not existing_membership:` (:1101) before either commits -- raises `sqlalchemy.exc.IntegrityError` on the losing `session.commit()`, which this `except` clause cannot catch: it propagates unhandled, through the outer `except Exception: session.rollback(); raise` at :1885-1889, and out of the dispatcher entirely, rather than being caught and logged as "Membership already exists" at :1117. `psycopg2.IntegrityError` is a genuine top-level re-export (`psycopg2/__init__.py:57`), so the import is not a typo -- it is simply the wrong class for code written against a SQLAlchemy `Session`. This is the highest-value finding in this sub-project: it is the ONLY exception handler in the entire Follow/Accept/Reject scope, it exists specifically to make a known race condition safe, and it cannot do that job for a real ORM-raised violation. | **high** -- a race-safety handler that cannot catch the real-world exception class the race it exists to handle actually raises; under genuine concurrent load (two peers, or a retried Accept, landing close together) the intended graceful, idempotent "already a member" outcome becomes an unhandled exception instead | **measured**: the class-hierarchy fact itself, by executing `issubclass()` and inspecting `__mro__` for both classes in the running container -- not inferred from reading either library's source; **reading-level**: the production consequence under a genuine race, since Task 6's own `test_a_membership_race_is_caught_as_an_integrity_error` drives the `except` clause by patching `session.add` to raise `psycopg2.IntegrityError` directly (necessarily catchable, being that exact class), which cannot discriminate this defect -- no test in this sub-project forces a real concurrent `session.commit()` |
| D73 | `process_inbox_request`, Follow/User target block guard (routes.py:1025) | **The third alternative of the block guard is dead under the default deny-list configuration.** `instance_banned(remote_user.instance.domain)` is unreachable via the ordinary path: the same `instance_banned` call inside `validate_remote_actor` (`app/activitypub/actor.py:68-69`), reached unconditionally from `find_actor_or_create_cached` at `app/activitypub/util.py:345` during the preamble's own actor resolution (routes.py:869, before the Follow branch is ever entered), already refuses a banned actor's signature before line 1025 can run. Task 4 confirmed this the direct way, not by assumption: seeding a bare `BannedInstances` row and dispatching produced `'Actor was not a user or a community' != 'Attempt to follow denied due to block'` -- the preamble's own refusal message, not this guard's. The alternative is reachable only under allowlist mode (`use_allowlist=True`), a genuine admin-facing configuration (`app/admin/routes.py:404-414`; 5a's D48 audit already established `AllowedInstances` and `BannedInstances` are edited independently with no cross-check), which routes actor validation through `instance_allowed()` instead, never consulting `BannedInstances`. Scoped exactly as the test docstring scopes it: **dead by default, not dead in general.** | informational -- dead code under the default configuration, reachable and meaningful under a real, if less common, admin configuration | **measured**: reachability under allowlist mode, via a 1-for-1 mutation kill isolated from the guard's other two alternatives (Task 4); **reading-level**, cross-referenced against 5a's own D48: the unreachability claim under the default deny-list configuration |
| D74 | `process_inbox_request`, Reject/user branch (routes.py:1183-1184) | **Reject's `existing_follow` query omits the `is_inward` filter that Accept's equivalent query (:1134-1136) applies.** Accept's analogous lookup is filtered to `is_inward=False`, guaranteeing it only ever touches the outward `UserFollower` row (the local user's own follow of the remote user). Reject's lookup applies no such filter. Reasoned consequence: a `UserFollower` row can exist for either direction between the same two users (`is_inward=True` from the Follow arm's own inward-follow write at :1032, `is_inward=False` from this same Accept/Reject arm's outward-follow bookkeeping); if both exist for the same `(local_user_id, remote_user_id)` pair, Reject's unfiltered `.first()` can match either one, potentially flipping the wrong direction's `is_accepted` to `False`. | medium (reasoned) -- a correctness risk conditioned on a real but narrower precondition (a mutual-follow pair existing between the same two users), not demonstrated to fire | **reading-level only**, self-disclosed in the test's own docstring: Task 8's `test_the_reject_user_branch_flips_an_existing_follow_and_decrements` docstring states plainly that it does not exercise this asymmetry |
| D75 | `process_inbox_request`, Reject/user branch (routes.py:1187) | `requestor_user.num_following -= 1` runs unconditionally whenever a `UserFollowRequest` join request is found, regardless of whether an `existing_follow` `UserFollower` row exists to decrement. Compare Accept's mirror-image increment (:1144), equally unconditional but only ever moving the counter up from a state Accept's own arm just created or confirmed -- Reject has no analogous guarantee, since a join request being present says nothing about whether a follow was ever actually accepted. A bare Reject with no prior acceptance (or a duplicate Reject) drives `num_following` negative, with nothing flooring it at zero. | low-medium -- data-integrity drift on a user-visible counter, peer-triggerable by sending a Reject with no corresponding acceptance | **measured**: `test_a_reject_decrements_num_following_even_with_no_follower_row` drives `num_following` from its seeded `0` to `-1` (Task 8) |

### 3. Two defects fixed under explicit authorisation -- D76-D77

Per the project owner's explicit instruction, this sub-project's Tasks 5 and
7 -- and only those two -- were authorised to fix the one defect each found
while writing its own tests, as a deliberate and bounded departure from this
campaign's report-don't-fix rule. Every other defect this sub-project found
(D64-D75 above, plus 5a's own D1-D63) was registered, not fixed. Both fixes
are single-identifier or single-guard changes, verified against the whole
suite, and are recorded here as fixed rather than open.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D76 | `process_inbox_request`, Accept (routes.py:1085) | **FIXED, commit `3e5d9a2f`.** The a.gup.pe string-Accept path (routes.py:1077-1085) could never succeed. The branch looked up the `CommunityJoinRequest` by the string's trailing segment and assigned its user to `user` instead of `requestor_user` -- backwards per the function's own comment two lines above (`requestor_user` is who made the Follow, `user` is who sent the Accept) -- so `requestor_user` stayed `None`, `if not requestor_user:` at :1091 was always true, and the path logged `'Could not find recipient of Accept'` and returned on every call, discarding the lookup's result. Pre-fix failure recorded directly: `AssertionError: assert member is not None` (no `CommunityMember` ever created). Fix: one identifier changed, `user =` to `requestor_user =`, at :1085. Full suite after the fix: 2845 passed, 3 skipped, 0 errors (an initial 1-error run was independently isolated and confirmed to be contamination from an unrelated deadlocked background run, not a regression -- `tests/test_activitypub_util.py` passed 5/5 both with and without the fix in isolation). | fixed and verified | **measured**: pre-fix failure captured directly; post-fix, Task 5's `test_an_agupe_string_accept_admits_the_join_requests_user` and `test_an_agupe_numeric_style_accept_retries_by_primary_key` (the latter also covering the :1081-1083 non-uuid retry path) both pass; full-suite run confirms no regression |
| D77 | `process_inbox_request`, Reject/user branch (routes.py:1179-1189) | **FIXED, commit `33167f20`.** Reject's user branch dereferenced an absent join request. When no `UserFollowRequest` existed between the two users (a Reject for a request that is already gone), `join_request` was `None`, and the very next line unconditionally read `join_request.user_id` -- `AttributeError: 'NoneType' object has no attribute 'user_id'` at routes.py:1183, uncaught. Fix: the branch body (the `existing_follow` query, the `is_accepted` flip, the `num_following` decrement, the commit, and the log call) was wrapped in `if join_request:`. **Corrected 2026-08-30 (fix-wave review): this does NOT match the sibling branches' structure**, as originally claimed. The sibling community (:1157-1168) and feed (:1169-1178) branches guard ONLY their `session.delete(join_request)` call with `if join_request:` -- their `session.commit()` and `log_incoming_ap(...)` run unconditionally, outside that guard, so a Reject naming an absent CommunityJoinRequest or FeedJoinRequest still logs `APLOG_SUCCESS`. Fix 2's guard is wider: it wraps the commit and the log call too, so a Reject naming an absent `UserFollowRequest` now writes zero `ActivityPubLog` rows instead of crashing. That side effect is real and previously unregistered -- see D78 below. `APLOG_ACCEPT` was left untouched everywhere, as instructed -- that mislabelling (D68 above) is a separate, deliberately out-of-scope finding. Full suite after the fix: 2859 passed, 3 skipped, no other test changed outcome. | fixed and verified | **measured**: pre-fix `AttributeError` captured directly from a real pytest failure; post-fix, `test_a_reject_for_a_missing_follow_request_is_handled` asserts the silent no-op (`ActivityPubLog.query.count() == 0`); full-suite run confirms no regression |

### 4. Two corrections from the fix-wave review -- D78-D79

Filed 2026-08-30, in the fix wave that closed this sub-project out. Both
correct a false claim made elsewhere in this section (D77's and D68's rows
above, both struck and cross-referenced in place), and both false claims
were concealing a real, previously-unregistered defect rather than merely
being imprecise.

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| D78 | `process_inbox_request`, Reject/user branch (routes.py:1179-1189) | D77's fix wrapped the user branch's `existing_follow` query, `is_accepted` flip, `num_following` decrement, `session.commit()` and `log_incoming_ap` call all inside one `if join_request:` guard. The sibling community (:1157-1168) and feed (:1169-1178) branches guard ONLY their `session.delete(join_request)` with `if join_request:` -- their `session.commit()` and `log_incoming_ap(...)` run unconditionally, so a Reject naming an absent `CommunityJoinRequest` or `FeedJoinRequest` still logs `APLOG_SUCCESS`. D77's wider guard means a Reject naming an absent `UserFollowRequest` now writes ZERO `ActivityPubLog` rows, where the two sibling targets still log a success row for the equivalent case. D77's fix converted an uncaught `AttributeError` into a silent no-op -- the same shape D65, D66 and D69 already register as defects. | low -- audit-trail asymmetry between the three Reject targets, same shape as D65/D66/D69; not a crash and not a regression from D77's own authorised remit (fixing the crash) | **measured**: `test_a_reject_for_a_missing_follow_request_is_handled` (`tests/test_inbox_dispatch_accept_reject.py:697`) asserts `ActivityPubLog.query.count() == 0`, currently pinning this silence as the expected outcome |
| D79 | `process_inbox_request`, Reject/user branch (routes.py:1180-1189) | Reject's user branch queries the `UserFollowRequest` at :1180-1181 but never deletes it, unlike the sibling community (:1161) and feed (:1172) branches, which both call `session.delete(join_request)`. The row survives every Reject. This compounds D75: because the row is never removed, a peer that repeats the same Reject finds the same `UserFollowRequest` again on every retry and decrements `num_following` again each time, so the counter can drift arbitrarily negative across repeated Rejects rather than by the single `-1` D75 measures from one call. | medium -- data-integrity drift on a user-visible counter, compounding D75, peer-triggerable by repeating a Reject | **measured**: the fix wave added an assertion to `test_the_reject_user_branch_flips_an_existing_follow_and_decrements` (`tests/test_inbox_dispatch_accept_reject.py`) confirming the `UserFollowRequest` row still exists after the Reject completes |

### 5. The allocation ledger, updated

D1-D20 sub-project 2a, D21-D24 sub-project 2b, D25-D29 sub-project 2c,
D30-D33 sub-project 2c's whole-branch review, D34-D40 sub-project 3, D41-D46
sub-project 4, D47 sub-project 4's whole-branch review, D48 the follow-on
audit of `instance_allowed`/`instance_banned`, D49-D63 sub-project 5a.
**D64-D77 this sub-project** (sections 2-3 above): D64 the Follow/Notification
session split; D65 Feed's silent reject; D66 the three silent already-related
paths; D67 `is_accepted` never `False` on the Follow path; D68 Reject's
`APLOG_ACCEPT` mislabelling; D69 Reject's silent non-Follow ignore; D70 the
unguarded `object['type']` reads in Accept and Reject; D71 the `User.query.get`
legacy/session read; D72 the `IntegrityError` handler bound to the wrong
exception class (this sub-project's highest-value finding); D73 the Follow
block guard's third alternative, dead by default; D74 Reject's missing
`is_inward` filter; D75 `num_following` drifting negative on Reject; D76-D77
Tasks 5 and 7's two authorised fixes. **D78-D79 the fix-wave review's two
corrections** (section 4 above): D78 the audit-trail asymmetry D77's fix
introduced; D79 the missing `UserFollowRequest` delete D68's correction
exposed. **Next free number: D80.**

## Sub-project 5c: the Delete, Lock, Add, Remove and Block arms of moderation

`docs/superpowers/specs/2026-08-30-coverage-inbox-moderation-5c-design.md`, on
branch `blentz`. Eleven test-writing tasks plus this report-only task covered
`process_inbox_request`'s five moderation arms directly downstream of 5a's
preamble and 5b's membership handshake: Delete (routes.py:1264-1330), Lock
(1360-1398), Add (1400-1471), Remove (1473-1574) and Block (1595-1675). 73
tests across three files: `tests/test_inbox_dispatch_lock_delete.py` (21,
Lock and Delete), `tests/test_inbox_dispatch_add_remove.py` (32, Add and
Remove) and `tests/test_inbox_dispatch_block.py` (20, Block). This
sub-project carried an **explicit, bounded exception** to the campaign's
report-don't-fix rule, larger than 5b's two: **the project owner separately
authorised six specific defect-fixes**, spread across four commits (Lock's
three identifiers in one commit; Delete's feed guard and actor guard in a
second; Add's loop guard in a third; Remove's feed-item guard and
membership guard in a fourth), while every other defect this sub-project
found was registered, not fixed. `git diff --stat app/` is empty for every
task except Tasks 2, 4, 6 and 8, each confined to the one arm its commit
names.

### 1. Whole-unit coverage, and every gap explained

Measured against this sub-project's own three files, per this task's brief:

```bash
./run_tests.sh tests/test_inbox_dispatch_lock_delete.py tests/test_inbox_dispatch_add_remove.py tests/test_inbox_dispatch_block.py \
  -q --cov=app.activitypub.routes --cov-report=json
```

73 passed, single foreground run, no hang. Module-level `percent_covered`
(23.07%, 446/1818 statements, 180/896 branches) is not comparable to 5a's or
5b's own blended figures for the same reason 5b already gave: it reflects
only what these three files exercise. The figures that matter are the five
spans' own numbers, intersected from `coverage.json`'s
`executed_lines`/`missing_lines`/`executed_branches`/`missing_branches`.

**The spec's span boundaries were stale, as the brief warned, and were
re-derived from source rather than trusted.** Every task in this
sub-project reported the same drift (Task 4's fix deleted Delete's dead
`else:` clause and de-indented its body, shifting every line after
:1300 for the rest of the sub-project). Corrected boundaries, each spanning
from the arm's own `if core_activity['type'] == '...':` line through its
final `return`:

| arm | spec said | corrected |
|---|---|---|
| Delete | 1264-1327 | **1264-1330** |
| Lock | 1356-1395 | **1360-1398** |
| Add | 1396-1468 | **1400-1471** |
| Remove | 1469-1570 | **1473-1574** |
| Block | 1590-1671 | **1595-1675** |

| span | statements | branches |
|---|---|---|
| Delete (1264-1330) | 47/47 executed | 26/26 executed, 0 missing arcs |
| Lock (1360-1398) | 29/29 executed | 16/16 executed, 0 missing arcs |
| Add (1400-1471) | 60/60 executed | 31/32 executed, 1 missing arc |
| Remove (1473-1574) | 77/77 executed | 43/46 executed, 3 missing arcs |
| Block (1595-1675) | 54/54 executed | 36/38 executed, 2 missing arcs |

**Every statement in all five spans is executed.** Zero missing lines --
between them the eleven test-writing tasks drove Delete's Feed/content/PM
trichotomy and every content-shape within it, both of Lock's post/comment
branches on both outcomes, Add's feed/sticky/moderator trichotomy, Remove's
mirror of the same trichotomy plus its five-condition auto-unsubscribe
loop, and Block's site-ban/community-ban/Mastodon trichotomy including both
permission guards' mutants. The only gaps are the six missing branch arcs
below, each explained rather than left as a remainder.

**The six missing branch arcs, in full:**

1. **Add, arc `[1419, 1415]` -- the per-member `if fm_user.is_local() and
   fm_user.feed_auto_follow:` False-skip, inside the feed branch's
   auto-subscribe loop (routes.py:1414-1423).** A real, narrow
   test-matrix gap, not a defect. Every feed member that reaches line 1419
   at all in this suite (the feed's owner is diverted by the `continue` at
   :1418 one line earlier, before ever reaching :1419) is local with
   `feed_auto_follow` True -- Task 7's
   `test_the_feed_branchs_success_path_subscribes_non_owners_and_logs_nothing`
   and Task 6's single-member fixture both build members that pass this
   check. No test in this suite seeds a non-owner feed member who is
   non-local, or local with `feed_auto_follow` False, so the guard's False
   arm (skip this member, loop to the next) is never taken. The code's own
   behaviour on that arm is unremarkable -- a silent skip, matching the
   already-registered "Add's silent feed branch" finding (D81 below) -- so
   this is scope the eleven test-writing tasks simply didn't spend on, not
   a defect this task is failing to explain.
2. **Remove, arc `[1481, 1574]` -- the feed branch's `if community_to_remove
   and isinstance(community_to_remove, Community):` False-skip
   (routes.py:1481).** A real, narrow test-matrix gap, and the direct
   mirror of a case Add's own Task 6 DID cover
   (`test_an_add_whose_community_cannot_be_resolved_does_not_touch_feed_members`).
   No Remove test sends a feed Remove naming a community that fails to
   resolve (or resolves to something that isn't a `Community`); every
   Remove test that reaches the feed branch's guard resolves a real,
   seeded Community (Task 8's `test_a_remove_for_a_community_not_in_the_feed_is_a_no_op`
   resolves a real Community that merely isn't in the feed, which is a
   different case -- `community_to_remove` itself is truthy there). The
   behaviour on the untaken arm is the same silent early-`return` shape
   Add's equivalent guard has, not a new defect.
3. **Remove, arc `[1522, 1491]` -- the `if proceed:` False-skip inside the
   auto-unsubscribe loop's Undo-sending block (routes.py:1522), looping
   back to the next feed member (:1491).** An **equivalent mutant / dead
   branch**, the same type-narrowing shape this document has used
   throughout (5a's D61/D62, 5b's Follow arc `[1018, 1072]`). Reading the
   arm: `proceed = True` (:1500) is set unconditionally, immediately on
   entry to the `if subscription != SUBSCRIPTION_OWNER and cm and
   cm.joined_via_feed:` block (:1499) that is the ONLY path to line 1522 --
   `if proceed:` sits at the same indentation level as :1500 and :1502,
   i.e. as a sibling inside that same block, and nothing between :1500 and
   :1522 (the `is_local()`/`gone_forever`/`ovo.st` nesting at :1502-1520)
   ever reassigns `proceed`. So by construction, every time line 1522 runs,
   `proceed` was just set `True` a few lines above on that same pass --
   the False arm cannot fire on any input. **New finding from this task's
   own branch analysis, registered as D95 below** (informational -- dead
   code, not a bug, since the variable exists purely as a vestige, plausibly
   from an earlier version of this loop that computed `proceed` in more
   than one place).
4. **Remove, arc `[1536, 1538]` -- the community branch's `if not
   community.ap_featured_url:` False-skip (routes.py:1536-1537), the
   backfill guard on the sticky-target path.** A real, narrow test-matrix
   gap, and the mirror of a case Add's own Task 7 DID cover
   (`test_sticky_with_a_pre_set_featured_url_is_not_backfilled`). No Remove
   test seeds a community whose `ap_featured_url` is already set before
   dispatch -- Task 10's
   `test_remove_unsticky_backfills_ap_featured_url_and_compares_case_insensitively`
   starts from `None` and exercises only the True arm (the backfill). The
   untested arm's behaviour (skip the backfill, use the pre-set value) is
   identical in shape to Add's own already-covered case.
5. **Block, arc `[1595, 1677]` -- the top-level `if core_activity['type']
   == 'Block':` False-skip, falling through to the `Undo` check at
   :1677.** Legitimately out of this sub-project's scope, the same
   reasoning 5b gave for Reject's arc `[1150, 1193]`: within this
   three-file suite, the only way to reach line 1595 at all is a genuine
   `Block`-type activity (every Delete/Lock/Add/Remove test returns from
   its own arm strictly before :1595 is reached). Driving the False arm
   needs a non-`Block` activity that survives every earlier arm's own
   `return`, which is exactly what 5a's, 5b's and this sub-project's own
   *other* test files already do for their own types -- a different
   sub-project's territory, not a gap in Block's own coverage.
6. **Block, arc `[1670, 1675]` -- the Mastodon path's `if 'object' in
   core_activity and isinstance(core_activity['object'], str):` False-skip
   (routes.py:1670).** An **equivalent mutant / dead branch**, established
   by Task 11's own dict-object probe. `core_activity['object']` is read
   unconditionally via `.lower()` at :1617, long before :1670 -- if
   `'object'` were absent from `core_activity`, or if it were anything
   other than a string (a dict, as Task 11's
   `test_a_dict_shaped_object_crashes_before_any_isinstance_check` pins;
   equally a list or any other non-string type, by the same `.lower()`
   crash), the arm would already have raised `KeyError` or `AttributeError`
   at :1617 and never reached :1670 at all. Nothing between :1617 and
   :1670 reassigns `core_activity['object']`. So by the time line 1670
   runs, `'object' in core_activity` and `isinstance(core_activity['object'],
   str)` are BOTH already guaranteed True -- the guard cannot fail on any
   input that survives to it. **New finding from this task's own branch
   analysis, registered as D96 below.**

### 2. New defects -- D80-D96

None fixed by this task, per its report-only remit -- including D95 and
D96, the two dead-code findings this task's own coverage analysis
surfaced. D97-D102 (section 3 below) are the six exceptions this
sub-project's own authorisation carved out, and none of those six was found
or fixed by this task; each was found and fixed by the task named in its
row. Severity is argued from what the tests actually executed wherever a
test exists; several rows below state explicitly which half of the claim is
measured and which is reasoned from source.

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| D80 | `process_inbox_request`, Remove/community branch (routes.py:1533, 1568, 1571, 1573) | **Four of Remove's `log_incoming_ap` calls pass `APLOG_ADD` instead of `APLOG_REMOVE`**: the permission-denied refusal (:1533), the moderators-url unresolvable-actor failure (:1568), the unknown-target failure (:1571), and the cannot-find-community-or-feed failure (:1573). Only three of the community branch's calls are correctly labelled `APLOG_REMOVE` (:1545 sticky success, :1547 sticky post-not-found, :1564 mod-removal success) -- a fourth `APLOG_REMOVE` call at :1529 belongs to the sibling feed branch's auto-unsubscribe loop, not this branch, and is not counted here (an earlier draft of this correction double-counted it; the ledger's corrected figures are used throughout this row). Same defect class as 5b's D68 (Reject's `APLOG_ACCEPT` mislabelling), here on the failure paths of Remove rather than every path of Reject. | cosmetic -- operator-facing mislabelling only; the refusal/failure behaviour itself is correct in every case | **measured**, four distinct tests, each asserting `log.activity_type == APLOG_ADD[1]` on a genuine Remove dispatch: Task 10's `test_remove_permission_denied_pins_the_aplog_add_mislabelling` (:1533), `test_remove_mod_unresolvable_actor_reports_cannot_find` (:1568), `test_remove_unknown_target` (:1571), `test_remove_with_neither_community_nor_feed_resolvable_is_refused` (:1573) |
| D81 | `process_inbox_request`, Add/feed branch (routes.py:1405-1423) | The entire feed branch -- creating a `FeedItem`, incrementing `feed.num_communities`, and auto-subscribing eligible feed members -- calls `log_incoming_ap` on no path at all: neither its success shape nor its unresolvable-community shape writes an `ActivityPubLog` row. A federated feed/Add leaves zero audit trail, in contrast to the community branch a few lines below, which logs every outcome. | low -- diagnostic/audit-trail gap, same shape as 5b's D65 (Follow/Feed's silent reject); no security or data-integrity impact | **measured**: Task 6's `test_an_add_whose_community_cannot_be_resolved_does_not_touch_feed_members` and Task 7's `test_the_feed_branchs_success_path_subscribes_non_owners_and_logs_nothing` both enable `LOG_ACTIVITYPUB_TO_DB` and assert `ActivityPubLog.query.count() == 0`, covering both reachable outcomes |
| D82 | `process_inbox_request`, Remove/feed branch (routes.py:1478-1530) | The mirror of D81 on the Remove side: the feed branch (FeedItem removal, per-member auto-unsubscribe loop, the Undo-sending block) calls `log_incoming_ap` on no path except the auto-unsubscribe loop's own internal success log (:1529, a distinct, already-correctly-labelled call scoped to Task 9's territory). The branch's own top-level outcomes -- a no-op when the community isn't in the feed, an unresolvable community -- log nothing. | low -- same shape as D81 | **measured**: Task 8's `test_a_remove_for_a_community_not_in_the_feed_is_a_no_op` enables logging and asserts `ActivityPubLog.query.count() == 0` for the no-op case |
| D83 | `process_inbox_request`, Block/Mastodon branch (routes.py:1669-1673) | The Mastodon no-target path (create a `UserBlock`, or silently skip a duplicate) calls `log_incoming_ap` on neither outcome -- confirmed by reading the branch in full, not merely "not observed to fire". Because `core_activity['object']` is read unconditionally three lines into the arm (:1617, see D87/the arc-6 dead-branch finding above), this silence also covers the dict-object crash outcome: EVERY no-target Block whose object isn't a string logs nothing, crash included. | low -- audit-trail gap, same shape as D81/D82; the crash half compounds with D87's severity rather than adding a new one | **measured**: Task 11's `test_mastodon_no_target_creates_a_block_and_logs_nothing` and `test_mastodon_no_target_skips_a_duplicate_and_logs_nothing` both assert `ActivityPubLog.query.count() == 0` with logging enabled |
| D84 | `process_inbox_request`, Remove/community branch (routes.py:1565-1566) | `add_to_modlog('remove_mod', actor=mod, target_user=old_mod, community=community, ...)` sits OUTSIDE the `if existing_membership:` guard (:1555-1564) that wraps the actual `is_moderator = False` write and its SUCCESS log -- so a Remove naming a moderator target that has no `CommunityMember` row at all still writes a modlog entry claiming a demotion happened, while the `ActivityPubLog` and the `CommunityMember` table both stay silent/unchanged. The modlog and the actual moderation state can disagree. | medium -- a moderation-audit record (modlog) that does not correspond to any actual moderation action or `ActivityPubLog` entry; peer-triggerable by naming a non-member in a Remove's moderators-url target | **measured**: Task 10's `test_remove_mod_without_existing_membership_writes_modlog_but_logs_nothing` asserts the modlog call fires (via `record_moderation`) while `ActivityPubLog.query.count() == 0` (logging enabled) |
| D85 | `process_inbox_request`, Delete/feed branch (routes.py:1286-1303) | The three feed-teardown loops (`FeedItem`, `FeedMember`, `FeedJoinRequest`) each call `session.commit()` inside the loop body, once per row, rather than once after the loop (or once for the whole deletion). A feed with N items, M members and K join requests issues N+M+K+1 commits (plus the final `session.delete(feed)`'s own commit) where one would do. | low -- inefficiency/N+1-commit pattern, not a correctness defect; no test demonstrates a partial-failure scenario where the per-item commits would matter (e.g. a crash mid-loop leaving some rows deleted and others not) | **measured** that the loop bodies genuinely execute more than once per test and every row is gone afterward (Task 5's `test_a_delete_of_a_feed_removes_items_members_and_join_requests` seeds 2 `FeedItem`s, 2 `FeedMember`s and 1 `FeedJoinRequest`, asserting all three tables are emptied); **reading-level** that the commits themselves are per-item rather than batched -- the test does not count `session.commit()` invocations, only the end state, which a single final commit would produce identically |
| D86 | `process_inbox_request`, Delete (routes.py:1321-1330) | When neither `find_liked_object` nor the `ChatMessage` lookup matches the deleted item's `ap_id`, the arm falls to its final `return` with zero writes, zero delegate calls, and zero logging -- a Delete for content this instance never had leaves no trace at all. | low -- silent no-op, same shape as this document's recurring "silent already-satisfied/unmatched" class (5b's D66, D81/D82/D83 above) | **measured**: Task 5's `test_delete_of_an_unmatched_ap_id_logs_nothing` enables `LOG_ACTIVITYPUB_TO_DB` and asserts `ActivityPubLog.query.count() == 0` |
| D87 | `process_inbox_request`, Block (routes.py:1617) | `blocked_ap_id = core_activity['object'].lower()` runs unconditionally, before `'target'` is even inspected, with no `isinstance` guard. A dict-shaped (or otherwise non-string) `object` raises `AttributeError` uncaught, three lines into the arm, before any of the three sub-paths' own type-checks (including the Mastodon path's own `isinstance` guard at :1670, see arc-6 above) ever run. | medium -- peer-triggerable, unhandled exception instead of a graceful refusal, same class this document gives throughout (5a's D49-D52/D55/D56, 5b's D70) | **measured**: Task 11's `test_a_dict_shaped_object_crashes_before_any_isinstance_check` asserts the exact `AttributeError` and that zero `ActivityPubLog` rows are written |
| D88 | `process_inbox_request`, Add (routes.py:1428) and Remove (routes.py:1535) | Both arms' community branch reads `target = core_activity['target']` with no `.get()`/`in` guard, immediately after the permission check passes. An Add or Remove whose activity omits `target` entirely raises `KeyError: 'target'`, uncaught, in both arms alike -- the same unguarded-peer-supplied-key shape this document generalises from D13/D30, 5a's D49-D52/D55/D56, and 5b's D70. | medium -- peer-triggerable, unhandled exception instead of a graceful refusal | **measured**, one test per arm: Task 7's `test_a_target_omitted_entirely_raises_keyerror` (Add, `pytest.raises(KeyError, match='target')`) and Task 10's `test_remove_target_omitted_entirely_raises_keyerror` (Remove, same assertion shape) |
| D89 | `process_inbox_request`, Remove/feed branch (routes.py:1505) | `if community_to_remove.instance.domain == 'ovo.st':` hardcodes one specific peer instance's domain to special-case how `follow_id` is generated (reusing a `CommunityJoinRequest`'s own `uuid` rather than a freshly generated one) when auto-unfollowing during a feed removal. Any other instance with the same underlying need (a peer that validates a Follow/Undo pair's `id` against the original join request) gets the generic, uncorrelated `follow_id` instead -- the special-casing does not generalise to "peers that need this," only to one hardcoded hostname. | informational -- a maintainability/generality concern, not demonstrated to break federation with `ovo.st` itself (the branch it drives IS correctly exercised) | **measured** that the branch fires exactly as coded for that hostname and reads the join request's `uuid` when present: Task 9's `test_remove_ovo_st_uses_the_join_requests_uuid_as_the_follow_id` and `test_remove_ovo_st_keeps_the_generated_follow_id_when_no_join_request_exists`; **reading-level** that the hardcoding itself is a defect worth generalising |
| D90 | `process_inbox_request`, Add/feed branch (routes.py:1421) | `from app.community.routes import do_subscribe` is an inline import placed inside the per-member `for fm in feed_members:` loop body, so it re-executes on every iteration rather than once at module load. Confirmed by Task 6: the import's binding site is not `app.activitypub.routes` at all (`grep` shows no such module-level attribute), which is why the test patches `app.community.routes.do_subscribe` directly rather than the routes module's own namespace. | informational -- negligible runtime cost (Python caches the imported module in `sys.modules`, so this is a repeated dict lookup, not a repeated disk read), but the loop-local placement is unusual style for this codebase and easy to overlook when auditing what `do_subscribe` resolves to at call time | **measured**: Task 6 confirmed the binding-site fact directly (patching the routes-module attribute has no effect; patching `app.community.routes.do_subscribe` does); **reading-level**: the "why is this inline at all, and why inside the loop rather than at the top of the branch" question itself |
| **D91** | `process_inbox_request`, Block, site-ban ordinary case (routes.py:1645, 1647) | **FIXED 2026-08-31, under explicit user authorisation, outside any sub-project -- see section 6 below for the fix and for the one claim in this row that turned out to be wrong.** **The most severe finding this sub-project made -- narrower in scope than first stated; see the 2026-08-31 correction below.** `blocked.ban_until = core_activity['expires']` (or `['endTime']`) writes to an attribute name `User` does not have. The real, mapped `DateTime` column is `banned_until` (`app/models.py:975`, commented "null == permanent ban"); `ban_until` is a real column, but on a completely different model, `CommunityBan` (`app/models.py:3545`). The write is therefore ordinary Python instance-attribute assignment, invisible to SQLAlchemy's flush machinery: it neither raises nor persists, and vanishes when the dispatcher's own independent task session closes. In Task 11's own words, quoted verbatim: **"In plain words: a remote TEMPORARY ban silently becomes a PERMANENT one, every time, because the column that would record its expiry is never written."** `blocked.banned` (the real, correctly-named boolean column) IS set `True` correctly on this path -- only the expiry half is lost, and `banned_until` stays at its NULL/"permanent" default forever as a result. Decisive evidence this is a typo, not an alternate convention: the Undo/Block (site-unban) path a few hundred lines below, reversing the identical kind of ban, correctly writes `unblocked.banned_until = None` (routes.py:1853) -- the person who wrote the reversal used the real column name; the person who wrote the ban did not.<br><br>**Corrected 2026-08-31 (fix-wave review): this row's claim, as first written, overreached -- it is not "every federated temporary site ban," only the ORDINARY site-ban shape (blocked user is remote AND on the blocker's own instance, routes.py:1642-1652).** Two sibling shapes on the SAME arm are unaffected: a site ban of a LOCAL user (:1632-1636) delegates entirely to `ban_user(blocker, blocked, None, core_activity)`, called just nine lines above the broken site (:1633 vs. :1642), which creates an `InstanceBan` row and correctly writes `instance_ban.banned_until = datetime.fromisoformat(core_activity['expires'])` (`app/activitypub/util.py:2393-2399`) -- expiry recorded correctly. This is the very branch D94 below already describes as calling `ban_user`; this row and D94 previously stood in unremarked contradiction over the same nine lines of source. A site ban of a user on a THIRD instance (:1637-1640) returns MONITOR without banning at all -- nothing is lost, because nothing is attempted. Community bans are unaffected too: they delegate to `ban_user`'s OTHER branch (`community is not None`), which writes `CommunityBan.ban_until` via `datetime.fromisoformat` with a `pendulum.parse` fallback, timezone normalisation (`tzinfo` defaulted to UTC) and past-date rejection (`if ban_until > datetime.now(timezone.utc):`) -- correct, and considerably more robust than either site-ban path.<br><br>**Two facts this row omitted, also added 2026-08-31:** first, the fix has TWO halves, not one. Task 11's own test-file docstring already says so correctly ("a one-word rename plus real parsing of the peer string"); this row, as first written, named only the wrong column. A reader acting on this row alone would rename `ban_until` to `banned_until` and push an unvalidated, peer-supplied ISO string straight into a `DateTime` column -- `ban_user`'s own `datetime.fromisoformat(...)` calls (above) are exactly the kind of call that raises on a malformed string, so a bare rename alone would trade a silently-discarded write for an unhandled crash, not fix the defect. Second, `ban_user`, called nine lines above the broken site, ALREADY implements the correct handling for this exact input -- the same `expires`/`endTime` fields, for the same kind of ban. That is both the strongest evidence this row's core claim is right (a working sibling branch nine lines away, doing the identical job correctly) and the best pointer to the likely correct fix: calling `ban_user` on this path too, the way the local-user branch above it already does, rather than renaming the broken attribute in place.<br><br>**Resolved 2026-08-31.** The ordinary site-ban path now calls `ban_user(blocker, blocked, None, core_activity)`, exactly as this row's closing sentence recommended, keeping its own `blocked.banned = True` alongside (see section 6 for why that line had to stay -- this row did not say so, and a reader following the recommendation literally would have dropped the global ban). Expiry parsing was extracted from `ban_user`'s community branch into `parse_ban_expiry` (`app/activitypub/util.py`) and is now shared by all three call sites. | **high, within its actual scope -- corrected 2026-08-31: not "every" federated temporary site ban, only the ORDINARY (remote, same-instance) site-ban shape.** Local-user site bans and all community bans are unaffected (see the corrected defect cell); third-instance site bans never attempt a ban at all. Within that narrower scope: no error, no log distinction, and no way for an admin reading `ActivityPubLog`'s unconditional SUCCESS row to know the expiry was discarded | **measured**, by direct observation rather than assumption: Task 11's `test_ordinary_site_ban_the_ban_until_probe` confirms via `sa_inspect(User).columns.keys()` that `'ban_until'` is not a mapped column at all; that `session.commit()` succeeds with a deliberately unparseable `expires` string, proving nothing downstream validates it; and that the real `banned_until` column, re-read fresh from the database after dispatch, is untouched at its seeded `None` |
| D92 | `process_inbox_request`, Delete (routes.py:1266) | `core_activity['object']['type'] == 'Feed'` reads the `'type'` key unconditionally once `isinstance(core_activity['object'], dict)` is True -- `isinstance` only short-circuits the case where `object` isn't a dict at all, not a dict missing that specific key. A dict-shaped object with no `'type'` key raises `KeyError`, uncaught, before either of the two already-fixed guards (D98/D99 below) or the arm's own success paths are ever reached -- a third, previously-unregistered crash path in Delete, distinct from both fixed defects. | medium -- peer-triggerable, unhandled exception instead of a graceful refusal, same class as D87/D88 | **measured**: Task 5's `test_delete_of_a_dict_object_with_no_type_key_raises_keyerror` asserts `pytest.raises(KeyError, match=r"^'type'$")` and that zero `ActivityPubLog` rows are written; first surfaced by Task 1 reading the arm, independently confirmed by Task 1's own reviewer |
| D93 | `find_community` (`app/activitypub/util.py:4568-4581`), reached from Add | `rj = request_json['object'] if 'object' in request_json else request_json` (:4568) is followed, a few lines later, by `rj.get('type') == 'Video'` (:4581) with no guard that `rj` is actually a dict -- a third crash site in this function, distinct from the two (`KeyError` on a missing `'type'`, `AttributeError` on `.startswith` against a non-string list element) already fixed as D2/D3, and from the third (`KeyError`/`TypeError` in the Video `attributedTo` walk) already registered as D28. A plain-string `object` -- the common shape for a real Lemmy sticky/add-mod Add -- drives this line into `AttributeError: 'str' object has no attribute 'get'`. `find_community` has exactly three call sites in `routes.py` -- Create/Update (`:1223`), Add (`:1404`) and Remove (`:1477`), verified directly against current source; Lock resolves its target through `Post.get_by_ap_id`/`PostReply.get_by_ap_id` and never calls `find_community` at all -- so the crash is reachable from all three of those callers, of which only Add and Remove fall inside this sub-project's own scope. | medium -- peer-triggerable, unhandled exception, same class as D2/D3/D28; not this sub-project's function to fix (out of scope: this defect lives in `util.py`, not the `routes.py` arms this sub-project's tasks were dispatched against) | **reading-level, with a directly observed crash**: Task 7's report records hitting exactly this `AttributeError` while building `test_add_with_neither_community_nor_feed_resolvable_is_refused`, and rewrote that test to send a dict `object` instead so it would exercise the Add arm's own final `else` rather than this separate bug -- the crash was real and observed, but no committed test pins it with `pytest.raises` (the committed test deliberately avoids triggering it) |
| D94 | `process_inbox_request`, Block, site-ban "blocked is local" branch (routes.py:1632-1636) | Unlike the "ordinary" (blocked-on-a-different-instance) site-ban branch a few lines below, which explicitly skips the `banned`/`ban_until` write `if not already_banned`, the "blocked is local" branch calls `ban_user(blocker, blocked, None, core_activity)` UNCONDITIONALLY -- `already_banned` (computed at :1622-1623 specifically, per the arm's own docstring, "we don't want remote temp bans to over-ride our permanent bans") is never consulted on this branch at all. A remote admin re-sending an already-actioned ban of a local user always re-invokes the local `ban_user` delegate. | low-medium -- redundant delegate invocation on every re-send, inconsistent with the sibling branch's explicit intent to avoid exactly this, though `ban_user`'s own idempotency (not audited by this sub-project) may or may not make the redundant call harmless | **measured**: Task 11's `test_site_ban_of_a_local_user_reinvokes_ban_user_even_if_already_banned` seeds `blocked.banned = True` beforehand and asserts `ban_user` is still called |
| D95 | `process_inbox_request`, Remove/feed branch (routes.py:1522) | **New finding, from this task's own branch analysis (arc `[1522, 1491]`, section 1 above).** `if proceed:` is an equivalent mutant / dead branch: `proceed = True` is set unconditionally at :1500, on the only code path (the `if subscription != SUBSCRIPTION_OWNER and cm and cm.joined_via_feed:` block at :1499) that ever reaches :1522, and nothing between the two lines reassigns it. The False arm cannot fire on any input. | informational -- dead code, not a bug; harmless as written, but a maintenance trap if a future edit adds a path to :1522 that does NOT pass through :1499-1500 without noticing `proceed`'s only assignment is upstream | **reading-level**: derived directly from this task's own coverage.json missing-branches output plus source inspection of the guard structure; no test in this sub-project specifically targets this arc, consistent with it being unreachable |
| D96 | `process_inbox_request`, Block/Mastodon branch (routes.py:1670) | **New finding, from this task's own branch analysis (arc `[1670, 1675]`, section 1 above).** `if 'object' in core_activity and isinstance(core_activity['object'], str):` is an equivalent mutant / dead branch, given D87 above: `core_activity['object']` is already read unconditionally via `.lower()` at :1617, so any activity that survives to :1670 already has a string `'object'` -- the guard cannot fail on any input that reaches it. | informational -- dead code, not a bug; the guard reads as defensive but the crash it appears to guard against (D87) already happens 53 lines earlier | **measured, obliquely**: Task 11's own dict-object probe (`test_a_dict_shaped_object_crashes_before_any_isinstance_check`) demonstrates the mechanism that makes this arc dead (the earlier `.lower()` crash), though no test targets :1670's guard directly |

### 3. Six defects fixed under explicit authorisation -- D97-D102

Per the project owner's explicit instruction, this sub-project was
authorised to fix exactly six specific defects, found while Tasks 2, 4, 6
and 8 wrote their own tests -- a deliberate, bounded departure from this
campaign's report-don't-fix rule, **larger than 5b's two-defect
authorisation**, reflecting this sub-project's wider scope (five arms
against 5b's three). Every other defect this sub-project found (D80-D96
above, plus 5a's D1-D63 and 5b's D64-D79) was registered, not fixed. All
six fixes are single-identifier or single-guard changes, each verified
against the whole suite by the controller after its task committed, and
are recorded here as fixed rather than open.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D97 | `process_inbox_request`, Lock/comment branch (routes.py:1380, 1386, 1387 pre-fix) | **FIXED, commit `b79f43f9`.** In `elif post_reply:`, the branch referenced `post` -- guaranteed `None` here, since this branch only runs when the `if post:` branch above did not -- at all three of its textual mentions: the permission check's second `or` operand (:1380), `target_user=post.author` (:1386), and `community=post.community` (:1387). Python's short-circuit evaluation meant only one of the three ever executed per call: if the actor genuinely was a moderator/admin, the `replies_enabled` write and the raw-SQL subtree `UPDATE` DID happen and committed, then :1386 crashed with `AttributeError: 'NoneType' object has no attribute 'author'` before `add_to_modlog` or the SUCCESS log ran, leaving the write stranded with no record; if not, :1380 crashed immediately with `AttributeError: 'NoneType' object has no attribute 'community'`, before any write. A federated comment-Lock could complete neither successfully nor with a clean refusal. Fix: all three `post` references changed to `post_reply`. Full suite after the fix: 2872 passed, 3 skipped, no other test broken. | fixed and verified | **measured**: both pre-fix `AttributeError`s captured directly from real pytest failures; post-fix, Task 2's `test_a_moderator_can_lock_a_comment` (success) asserts BOTH `parent_reply.replies_enabled is False` AND `child_reply.replies_enabled is False` via a genuinely path-matching seeded reply, proving the raw-SQL subtree `UPDATE` -- which had never executed against a working branch before this fix -- now runs correctly; `test_a_non_moderator_locking_a_comment_is_refused` (refusal) asserts both stay `True` |
| D98 | `process_inbox_request`, Delete/feed branch (routes.py:1273 pre-fix, now guarded at :1272-1275) | **FIXED, commit `c1bae61c`.** `if not user.id == feed.user_id:` dereferenced `feed.user_id` with no guard that the named Feed resolved at all -- an unknown `ap_public_url` left `feed` as `None`, crashing with `AttributeError: 'NoneType' object has no attribute 'user_id'`. Fix: an `if not feed:` guard, logging `APLOG_FAILURE` with the same message the removed dead `else:` clause used to emit, inserted before the ownership check. | fixed and verified | **measured**: pre-fix `AttributeError` captured directly (`routes.py:1273`); post-fix, Task 4's `test_a_delete_naming_an_unknown_feed_is_refused` asserts the FAILURE log and that the feed table is untouched |
| D99 | `process_inbox_request`, Delete/feed branch (routes.py:1273 pre-fix, now guarded at :1272-1275) | **FIXED, commit `c1bae61c`** (same commit as D98, a distinct guard). The same `if not user.id == feed.user_id:` line also dereferenced `user.id` with no guard that the sending actor resolved -- an unresolvable `actor_id` on the arm's own re-lookup left `user` as `None`, crashing with `AttributeError: 'NoneType' object has no attribute 'id'`. Fix: an `if not user:` guard, logging `APLOG_FAILURE` with a new message ('Delete rejected, could not find the sender.'), inserted immediately before the `if not feed:` guard. | fixed and verified | **measured**: pre-fix `AttributeError` captured directly (`routes.py:1273`, same line as D98, distinguished by which operand was `None`); post-fix, Task 4's `test_a_delete_from_an_unresolvable_actor_is_refused` asserts the new FAILURE message, reached via a stateful `find_actor_or_create_cached` stub that resolves the preamble's own lookup but not the arm's re-lookup |
| D100 | `process_inbox_request`, Add/feed branch (routes.py:1422 pre-fix) | **FIXED, commit `307d50a8`.** The feed-member auto-subscribe loop (querying `FeedMember`s and calling `do_subscribe` for each eligible one) sat OUTSIDE the `if community_to_add and isinstance(community_to_add, Community):` guard, so it ran even when the named community never resolved, crashing on `community_to_add.ap_id` with `AttributeError: 'NoneType' object has no attribute 'ap_id'`. Fix: the loop (comment, query and body, byte-identical apart from one added indentation level) moved inside the guard. | fixed and verified | **measured**: pre-fix `AttributeError` captured directly (`routes.py:1422`); post-fix, Task 6's `test_an_add_whose_community_cannot_be_resolved_does_not_touch_feed_members` asserts no `FeedItem` was created and `do_subscribe` was never called; re-mutated and re-run in Task 6's fix round to confirm the identical failure reproduces when de-indented, then restored |
| D101 | `process_inbox_request`, Remove/feed branch (routes.py:1484 pre-fix) | **FIXED, commit `5edb87b6`.** `session.delete(feed_item)` ran unconditionally after the `FeedItem` lookup, with no guard that a matching row existed -- a community not actually in the feed left `feed_item` as `None`, crashing with `sqlalchemy.orm.exc.UnmappedInstanceError: Class 'builtins.NoneType' is not mapped`. Fix: an `if feed_item:` guard wraps the delete, the `num_communities` decrement, and the commit (byte-identical apart from one added indentation level). | fixed and verified | **measured**: pre-fix `UnmappedInstanceError` captured directly; post-fix, Task 8's `test_a_remove_for_a_community_not_in_the_feed_is_a_no_op` asserts no crash, no `FeedItem` created, and `feed.num_communities` unchanged from its seeded nonzero baseline |
| D102 | `process_inbox_request`, Remove/feed branch (routes.py:1498 pre-fix) | **FIXED, commit `5edb87b6`** (same commit as D101, a distinct guard). `if subscription != SUBSCRIPTION_OWNER and cm.joined_via_feed:` dereferenced `cm.joined_via_feed` with no guard that a matching `CommunityMember` row existed -- a feed member with no membership in the community being removed left `cm` as `None`, crashing with `AttributeError: 'NoneType' object has no attribute 'joined_via_feed'`. Fix: the condition's middle conjunct became `cm and cm.joined_via_feed` -- a single inserted `cm and`, nothing else on the line changed. | fixed and verified | **measured**: pre-fix `AttributeError` captured directly (`routes.py:1498`); post-fix, Task 8's `test_a_remove_skips_a_feed_member_with_no_community_membership` asserts no crash and, via `record_sends`, that the loop stopped before any Undo/Follow send could fire -- proving the guard stopped the loop, not merely the crash |

### 4. One correction and one new finding from the fix wave -- D103

Filed 2026-08-31, in the fix wave that closed this sub-project out. D91's
row above (section 2) was corrected in place, narrowing its scope and
adding the two facts it had omitted -- struck-through nowhere (the row's
core claim, that the write is lost, remains true), but its severity cell's
"every federated temporary site ban" language is now qualified, and the row
is cross-referenced against D94, which it had previously, unremarkedly,
contradicted. The row below is new: a dead branch this sub-project's own
D95/D96 already established the class for, whose deadness turns out to be
a direct consequence of D91.

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| D103 | `process_inbox_request`, Block, site-ban ordinary case (routes.py:1642) | **CLOSED 2026-08-31 by D91's fix, exactly as this row predicted it would be.** `if not already_banned:` is no longer an equivalent mutant: the guard now skips a real `ban_user` call, and `test_site_ban_already_banned_skips_rebanning_but_still_succeeds` fails if it is removed (demonstrated by mutation, not asserted). The rest of this row describes the state of the code BEFORE that fix. `if not already_banned:` was, in the source this row was written against, an equivalent mutant / dead branch, given D91 above. Because `blocked.ban_until = ...` (:1645/1647) never reaches a real column (D91), the guard's only remaining observable effects are skipping that already-dead write and a `session.commit()` that has nothing new to flush. `blocked.banned` is already `True` on this path regardless, and both the unconditional SUCCESS log (:1652) and `site_ban_remove_data` (:1650-1651, its own separate `if remove_data:`) sit OUTSIDE this guard entirely. Deleting the guard and de-indenting its body changes nothing observable. Same class as D95 (Remove's dead `if proceed:`) and D96 (Block's dead Mastodon-path `isinstance` guard) above. **Cross-reference D91: the deadness here is a CONSEQUENCE of D91, not an independent coincidence.** If D91's fix landed (calling `ban_user` on this path, per D91's corrected pointer), `blocked.ban_until`/`banned_until` would become a real, meaningful write again, and this guard would stop being dead -- whoever fixes D91 should re-examine this guard rather than assume it stays equivalent. | informational -- dead code, not a bug on its own; a warning for whoever fixes D91, since the fix would resurrect this guard's meaning | **measured, obliquely**: Task 11's `test_site_ban_already_banned_skips_rebanning_but_still_succeeds` demonstrates the surviving behaviour (`banned_until` unchanged, SUCCESS still logged, `remove_data` still runs) -- its docstring originally attributed this to the guard itself; corrected in this fix wave (see `tests/test_inbox_dispatch_block.py`) to attribute it to D91 instead, with this row cross-referenced; **reading-level** for the equivalent-mutant claim itself, derived by tracing every use of the guard's write target through D91's confirmed non-existence of the `ban_until` column |

### 5. The allocation ledger, updated

D1-D20 sub-project 2a, D21-D24 sub-project 2b, D25-D29 sub-project 2c,
D30-D33 sub-project 2c's whole-branch review, D34-D40 sub-project 3, D41-D46
sub-project 4, D47 sub-project 4's whole-branch review, D48 the follow-on
audit of `instance_allowed`/`instance_banned`, D49-D63 sub-project 5a,
D64-D77 sub-project 5b, D78-D79 5b's fix-wave review corrections.
**D80-D96 this sub-project** (section 2 above): D80 Remove's `APLOG_ADD`
mislabelling (four sites); D81 Add's silent feed branch; D82 Remove's
silent feed branch; D83 Block's silent Mastodon path; D84 the `remove_mod`
modlog entry written without a membership; D85 Delete's per-item commits in
three loops; D86 Delete's silent unmatched-`ap_id` path; D87 Block's
unconditional `.lower()` on a possibly-non-string object; D88 the unguarded
`target` reads in Add and Remove; D89 Remove's hardcoded `ovo.st` special
case; D90 Add's per-iteration inline `do_subscribe` import; **D91 the
`ban_until`/`banned_until` defect, this sub-project's most severe
finding (scope corrected 2026-08-31, see section 4)**; D92 Delete's third
crash path (`['type']` on a dict with no such key); D93 `find_community`'s
third crash site, reached from Add; D94 the site-ban local-user branch
ignoring `already_banned`; D95 Remove's dead `if proceed:` branch; D96
Block's dead Mastodon-path `isinstance` guard. **D97-D102 the six
explicitly-authorised fixes** (section 3 above): D97 Lock's comment branch
(`b79f43f9`); D98-D99 Delete's feed and actor guards (`c1bae61c`); D100
Add's loop guard (`307d50a8`); D101-D102 Remove's feed-item and membership
guards (`5edb87b6`). **D103 the fix wave's new finding** (section 4 above):
the site-ban `already_banned` guard, a dead branch consequent on D91.
**Next free number: D104.**

### 6. D91 fixed, D103 closed -- 2026-08-31, outside any sub-project

Authorised directly by the user after sub-project 5c closed ("fix the
ban_until bug in routes.py"), so this sits outside 5c's no-fix contract
rather than violating it. No new D-numbers: this section resolves D91 and
closes D103. **Next free number is still D104.**

**The fix.** `app/activitypub/routes.py`'s ordinary site-ban branch now
reads:

```python
if not already_banned:
    blocked.banned = True
    # ban_user records the expiry on an InstanceBan row.
    # It does not set User.banned, so that stays here.
    ban_user(blocker, blocked, None, core_activity)
    session.commit()
```

**One claim in D91 was wrong, and it mattered.** D91's closing sentence
offered calling `ban_user` as "the likely correct fix ... rather than
renaming the broken attribute in place". That is the option taken, and it
is right -- but D91 presented it as a straight swap for the broken lines,
and it is not. `ban_user`'s instance-wide branch (`community is None`)
writes an `InstanceBan` row and **never touches `User.banned`**. Replacing
the whole `if not already_banned:` body with a `ban_user` call, which is
what D91's wording invites, would have silently dropped the global ban this
path exists to apply, trading a lost expiry for a lost ban. `blocked.banned
= True` therefore stays, with a comment saying why, and both halves are
asserted separately by
`test_ordinary_site_ban_delegates_to_ban_user_like_the_local_branch_above`.

Anyone reading D91's recommendation elsewhere should read this paragraph
with it.

**Expiry parsing, extracted rather than duplicated.** D91 correctly noted
the fix has two halves and that a bare rename would "trade a silently-
discarded write for an unhandled crash". Three call sites had three
different levels of care: the community branch of `ban_user` had the
hardened form, its instance branch used a bare `datetime.fromisoformat`,
and the Block arm did no parsing at all. The hardened form is now
`parse_ban_expiry(core_activity)` in `app/activitypub/util.py`, used by all
three. It returns a timezone-aware datetime, or None when the activity
carries no expiry, when the value cannot be parsed, or when it has already
passed.

**Malformed input no longer raises, on any of the three paths.** This is a
deliberate behaviour change beyond the literal defect, and it extends to
`ban_user`'s community branch, which previously let `pendulum.parse`
propagate. `process_inbox_request` wraps every arm in `except Exception:
session.rollback(); raise`, so a parse error propagating out of a ban
discards the entire ban -- meaning a peer could void any ban of their own
users by sending a malformed date. An unparseable expiry is now treated as
no expiry, which every consumer of these columns already reads as a
permanent ban (`User.banned_until`, `app/models.py:975`: "null == permanent
ban"). Losing an expiry is the lesser failure and the one the surrounding
code already models. It is, however, a silent one: nothing logs that a peer
sent an unparseable date. Registering that observation here rather than
adding a log this fix was not asked for.

**Tests.** `tests/test_activitypub_ban_expiry.py` (new, 20 tests) covers
`parse_ban_expiry` as a pure unit -- 'Z' suffixes, naive values assumed
UTC, non-UTC offsets preserved as the same instant, `expires`/`endTime`
precedence, past dates rejected at second granularity, unparseable and
non-string input, and the `pendulum` fallback. That last one is what keeps
the fallback from becoming dead code: `'2099-001'` (an ISO ordinal date) is
the only input in the file that `datetime.fromisoformat` rejects and
`pendulum.parse` accepts, verified by probe in this project's own
container, so deleting the `pendulum.parse` call fails exactly one test.

In `tests/test_inbox_dispatch_block.py`, the two tests that pinned the
broken behaviour are gone. `test_ordinary_site_ban_the_ban_until_probe` and
`test_ordinary_site_ban_reads_endTime_when_expires_is_absent` are replaced
by four: one asserting the delegation and its arguments against a double,
two driving the REAL `ban_user` end to end and reading the resulting
`InstanceBan.banned_until` back from the database, and one proving an
unparseable `expires` still lands a permanent ban instead of rolling the
whole thing back. The two `remove_data` tests now double `ban_user` as well,
so they stay about `remove_data` alone.

**Mutation-verified, both directions.** Reverting the fix to the original
`blocked.ban_until = ...` lines fails four tests; replacing `if not
already_banned:` with `if True:` fails exactly one, which is what closes
D103.

**Not done, deliberately.** D94 (the local-user branch calling `ban_user`
unconditionally, ignoring `already_banned`) is untouched -- it is a
separate defect on a different branch, and was not part of this
authorisation. Note that the two branches are now inconsistent in a newly
visible way: both call the same delegate, but only the ordinary one guards
the call.



## Sub-project 5d: the `Undo` arm -- Follow, Delete, Like/Dislike/Announce, ChooseAnswer, Lock and Block

`docs/superpowers/specs/2026-08-31-coverage-inbox-undo-5d-design.md`, on
branch `blentz`. Eleven tasks brought `process_inbox_request`'s `Undo` arm
(`app/activitypub/routes.py:1676-1881`) from 1 executed statement of 160 to
complete statement coverage, across its Follow, Delete, Like, Dislike,
Announce, ChooseAnswer, Lock and Block sub-types, plus the unmatched-type
fall-through -- nine paths in all, in three files:
`tests/test_inbox_dispatch_undo_follow.py`
(7 tests, Tasks 2-3), `tests/test_inbox_dispatch_undo_content.py` (17 tests,
Tasks 4-7 plus this task's Fix 4 test), and `tests/test_inbox_dispatch_undo_moderation.py`
(19 tests, Tasks 8-10 plus this task's four gap-closing tests) -- 43 tests
total. Like 5b and 5c before it, this sub-project carried an **explicit,
bounded exception** to the campaign's report-don't-fix rule: **the project
owner separately authorised four specific defect-fixes** -- D105, D106,
D107 and D109 -- while every other defect found was registered, not fixed.
A fifth fix, D108, was authorised separately by the controller, not the
owner's original four: it repairs a regression that D105's own fix (Fix 2)
introduced, discovered only after that fix landed. `git diff --stat app/`
is empty for every task except Task 9 (three commits -- the Undo/Lock
fixes, D105/D106/D107, plus D108's own regression repair) and this task
(one commit, Fix 4/D109).

### 1. Coverage after this task

Measured against this sub-project's own three files:

```bash
./run_tests.sh tests/test_inbox_dispatch_undo_follow.py tests/test_inbox_dispatch_undo_content.py tests/test_inbox_dispatch_undo_moderation.py \
  -q --cov=app.activitypub.routes --cov-report=json
```

43 passed. Intersecting `coverage.json`'s `executed_lines`/`missing_lines`
against the arm's span (`app/activitypub/routes.py:1676-1881`, from `if
core_activity['type'] == 'Undo':` through the arm's own final statement, the
`log_incoming_ap(..., 'Unmatched activity')` call that ends its
unmatched-type fall-through -- there is no `return` there; control falls
straight through to the next top-level type check, `QuoteRequest`):
**161 of 161 statements executed, zero missing.**

Before this task, the controller's own measurement (after Task 10, against
the pre-Fix-4 source) was reported as 161 of 168 statements, but that figure
was measured over `1676-1890`, a span that also includes the entire
`QuoteRequest` arm (5 statements, already fully covered by an earlier
sub-project) -- comparing it against this task's own 161-of-161 compares two
different spans. Measured honestly, against the `Undo` arm alone and
verified against the `b2380bc4` blob (the state of `routes.py` after Task 9's
three fixes, immediately before this task's Fix 4): **163 statements, 7
missing, 156 executed.** The true before/after is **156/163 -> 161/161**.
Two separate changes explain the arithmetic, and their meeting at the same
number, 161, either side of the arrow is a coincidence, not a reconciliation:
Task 9's three fixes (D105-D108) had already added 3 statements to the arm
before this task started (160 baseline -> 163), by turning a bare `else:`
into `if not post and not post_reply:` (D107, +1) and adding the two-line
`PostReply` fallback under `/post/` (D108, +2); this task's own Fix 4 (D109,
section 3 below) then removed 2 statements -- the one permanently-dead
`isinstance(core_activity['object'], str)` branch's dead assignment, plus the
now-unneeded `if`/`else` structure around it -- taking the arm from 163 down
to 161. The seven statements missing at 156/163 (routes.py:1788-1790, 1814,
1821-1822, 1873) split six-and-one between the two fixes: the four new tests
this task adds to `tests/test_inbox_dispatch_undo_moderation.py` close six
of them by executing them for the first time -- the three-statement
`Undo`/`Lock` `else:` fallback (1788-1790), the `post_reply` branch's own
permission-denied log (1814), and the two `cc`-emptying statements in
`Undo`/`Block` (1821-1822); the seventh, the `ChooseAnswer` branch's
permanently-dead statement (1873), is closed by Fix 4 deleting it outright,
not by a test covering it.

Five branch arcs remain unexercised within the span, none of them this
task's assignment: this task's own scope was the three named statement
gaps closed above (the `Undo`/`Lock` `else:` fallback, its `post_reply`
permission-denied log, and `Undo`/`Block`'s `cc`-emptying), not a
branch-coverage sweep of the whole arm. Listed here, consistent with 5c's
own "coverage gaps explained" convention, rather than left as a silent
remainder:

1. **`[1676, 1883]` -- the arm's own top-level `if core_activity['type'] ==
   'Undo':` False-skip**, falling through to the next top-level type check
   (`QuoteRequest`). Same reasoning 5b gave for Reject's arc `[1150, 1193]`
   and 5c gave for Block's arc `[1595, 1677]`: within these three
   Undo-only files, every test that reaches line 1676 at all sends a
   genuine `Undo` activity. Driving the False arm is a different
   sub-project's territory.
2. **`[1700, 1703]` and `[1703, 1705]` -- the Undo/Follow Feed branch's
   `if member:` and `if join_request:` False-skips** (routes.py:1700,
   1703). A narrow test-matrix gap: Task 3's
   `test_undo_follow_of_a_feed_removes_membership_and_join_request` is the
   only test that reaches this branch, and it seeds both a `FeedMember` and
   a `FeedJoinRequest`, so both `if`s take their True arm every time. The
   direct mirror of the Community branch's own two independent guards
   (`[1685]`/`[1690]`), which Task 2 drove on both sides with two separate
   tests -- no test here does the same for the Feed branch's pair.
3. **`[1719, 1721]` -- the Undo/Follow branch's trailing `if not target:`
   False-skip**, falling straight to `return` with no log call. Reachable
   only when `target` is truthy but matched none of the three preceding
   `isinstance` checks (`Community`, `Feed`, `User`) -- `find_actor_or_create_cached`
   is not documented to return anything else, so this is likely
   an equivalent mutant / dead branch rather than a live gap, but this task
   did not run the analysis 5c ran for D95/D96 to confirm it, so it is
   flagged rather than claimed.
4. **`[1757, 1762]` -- the Undo/Like-or-Dislike branch's `if not announced:`
   False-skip** (routes.py:1757), suppressing the `announce_activity_to_followers`
   call when the Undo arrived wrapped in an Announce. The direct sibling of
   the Undo/Delete announce-suppression guard Task 4 covered on both sides
   (`test_undo_delete_inside_an_announce_does_not_announce_again`); no
   equivalent Announce-wrapped test exists for Undo/Like or Undo/Dislike in
   this suite.

### 2. New defect -- D104

| # | function | defect | severity | evidence |
|---|---|---|---|---|
| D104 | `process_inbox_request`, `Lock` arm (routes.py:1360-1398, sub-project 5c's territory) | **Not fixed, registered only.** The sibling `Lock` arm (a federated *lock*, as opposed to the `Undo`/`Lock` arm this sub-project covers, which *unlocks*) has the identical missing `/post/`-to-`PostReply` fallback that D108 below fixes on the `Undo` side. A NodeBB-style reply whose `ap_id` contains `/post/` (`app/activitypub/util.py:1984` names NodeBB, the software -- not a hostname -- by name, as a known, misleading hint) cannot be locked via a federated `Lock` activity, for the same reason it could not be unlocked before D108's fix: the `/post/` branch tries only `Post.get_by_ap_id` and never falls back to `PostReply.get_by_ap_id` on a miss. Deliberately left alone here -- it belongs to sub-project 5c, and 5d's own fix (D108) did not introduce it; both canonical resolvers this document has referenced before (`find_reply_parent`, `_find_liked_object_id`, both `app/activitypub/util.py`) keep the fallback that both `Lock` arms lack. | medium -- peer-triggerable functional gap (a real class of remote reply, misrouted, cannot be locked at all), same severity class as the fixed D108 below, whose commit message and Task 9's report both name this exact sibling gap explicitly as out of scope | **reading-level**: Task 9's report (`.superpowers/sdd/2026-08-31-coverage-inbox-undo-5d/task-9-report.md`) states the gap and its deliberate exclusion directly, cross-referenced from `tests/test_inbox_dispatch_undo_moderation.py`'s own `test_a_nodebb_reply_whose_url_contains_post_still_falls_back_to_the_reply` docstring; no test in this suite (5d's own scope is `Undo`/`Lock`, not `Lock`) exercises the sibling arm to confirm the crash/miss directly, so this is derived from reading `Lock`'s source (routes.py:1360-1398) alongside `Undo`/`Lock`'s pre-fix shape, not from a dispatched `Lock` activity |

### 3. Five defects fixed -- four under explicit authorisation, one a controller-authorised regression repair -- D105-D109

Per the project owner's explicit instruction, this sub-project was
authorised to fix four specific defects: D105 and D106 (one commit fixing
two at once -- the same shape 5c's D98/D99 and D101/D102 used), D107, and
D109, found while Tasks 8 and 9 pinned and then inverted the `Undo`/`Lock`
arm's own tests, and while this task removed `Undo`/`ChooseAnswer`'s dead
branch. **D108 was NOT one of the owner's four.** It is a regression that
D105's own fix introduced (see D108's row below), discovered only after
that fix landed, and was authorised separately by the controller as repair
of that regression -- not part of the owner's original authorisation. Five
distinct identifiers, four commits, in total. Every other defect this
sub-project found (D104 above) was registered, not fixed.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D105 | `process_inbox_request`, `Undo`/`Lock` (routes.py:1781, 1783 pre-fix) | **FIXED, commit `3afc7abd`** (with D106, same commit). `if '/post/' in core_activity['object']:` and `elif '/comment/' in core_activity['object']:` tested membership against `core_activity['object']` -- the inner `{'type': 'Lock', 'object': target_ap_id}` DICT, not `target_ap_id` itself. `in` against a dict tests key membership, not substring containment, and neither string is ever a key, so both branches were unreachable and every `Undo`/`Lock` fell to the `else`. Fix: both operands changed to `target_ap_id`. **Behaviour narrowing worth recording**: post-fix, a `/comment/`-shaped `target_ap_id` now selects `PostReply.get_by_ap_id` directly and never tries `Post.get_by_ap_id` first, whereas pre-fix (via the always-taken `else`) it tried `Post.get_by_ap_id` before falling back to `PostReply`. This is the right call -- it matches the same-shaped guard in `_find_liked_object_id` (`app/activitypub/util.py:2007-2022`), which also tries `PostReply` directly under `/comment/` with no `Post` attempt first -- but it is a real, durable change in lookup order, not merely a bugfix restoring old behaviour. | fixed and verified | **measured**: pre-fix, Task 8 confirmed directly (its report quotes `'/post/' not in activity['object']` and `'/comment/' not in activity['object']` both holding true against the dict) that neither branch's own membership test could ever succeed; post-fix, Task 9's `test_the_comment_url_branch_selects_a_reply_directly` (`tests/test_inbox_dispatch_undo_moderation.py`) asserts a `/comment/`-shaped target resolves the reply directly, with a decoy `Post` seeded at the identical `ap_id` left untouched -- proving the membership test now runs against the string, not the dict |
| D106 | `process_inbox_request`, `Undo`/`Lock`, `post_reply` branch (routes.py:1807-1808 pre-fix) | **FIXED, commit `3afc7abd`** (with D105, same commit). **The exact twin of D97** (5c's Lock-arm fix, `b79f43f9`), here on the `Undo`/`Lock` side: `add_to_modlog('unlock_post_reply', ..., target_user=post.author, ..., community=post.community, ...)` sat inside `if post_reply:`, reachable only when `post` is `None` (the mutually exclusive branch pair at routes.py:1781-1788) -- so `post.author` always raised `AttributeError: 'NoneType' object has no attribute 'author'`, after the reply's own `replies_enabled` write and raw-SQL subtree `UPDATE` had already committed. A federated comment-Unlock could complete neither successfully nor with a clean refusal. Fix: both references changed to `post_reply.author`/`post_reply.community`. | fixed and verified | **measured**: pre-fix, Task 8's report records the `AttributeError` observed directly, against a test (since superseded by Task 9's inversion of the same scenario, per this document's numbering convention of citing current test names) that left `add_to_modlog` real/undoubled, since its arguments are evaluated before any double is entered; post-fix, `tests/test_inbox_dispatch_undo_moderation.py`'s current `test_unlocking_a_comment_records_the_reply_author_and_community` asserts (via `sa_inspect(obj).identity[0]`, not `.id` -- the captured kwargs are objects from the dispatcher's own closed session) that the modlog call now receives the reply's own author and community |
| D107 | `process_inbox_request`, `Undo`/`Lock` (routes.py:1813 pre-fix) | **FIXED, commit `0905ba1a`.** The trailing `else:` was indented to bind to `if post_reply:` (routes.py:1800), not to the `post`/`post_reply` pair -- the two are independent sibling `if` statements, not `if`/`elif`. Consequence: a successful POST unlock (which never touches `post_reply`, leaving it `None`) logged SUCCESS from the `if post:` branch, then always fell into the misbound `else`, logging a second, contradictory FAILURE `'Unlock: post not found'` for the same activity. Fix: the `else:` replaced with `if not post and not post_reply:`. | fixed and verified | **measured**: pre-fix, Task 8's report records a test (since superseded by Task 9's inversion of the same scenario) that showed exactly 2 `ActivityPubLog` rows (SUCCESS then FAILURE) for one successful post unlock; post-fix, `tests/test_inbox_dispatch_undo_moderation.py`'s current `test_a_successful_post_unlock_logs_success_and_nothing_else` asserts exactly 1 row, paired with `test_an_unlock_of_something_that_exists_nowhere_logs_not_found` asserting the FAILURE log's remaining, correct reason to exist |
| D108 | `process_inbox_request`, `Undo`/`Lock`, `/post/` branch (routes.py, post-D105 regression) | **FIXED, commit `9bfe5d5a`.** A regression D105's own fix introduced: making the `/post/`/`/comment/` membership test read the real string (D105) removed the `PostReply` fallback the buggy `else` had accidentally been providing for every `/post/`-shaped id, including NodeBB-style replies whose `ap_id` contains `/post/` with no matching `Post` row -- `app/activitypub/util.py:1984` names NodeBB, the software, by name as this misleading hint's source ("no hint in in_reply_to, or it was misleading (e.g. replies to nodebb comments have '/post/' in them)"), and both canonical resolvers in this codebase (`find_reply_parent`, `_find_liked_object_id`, both `app/activitypub/util.py`) keep a `PostReply` fallback under a `/post/`-shaped id for exactly this reason. Fix: the `/post/` branch gained the same two-line `if post is None: post_reply = PostReply.get_by_ap_id(...)` fallback the `else` branch already had. Scoped to `Undo`/`Lock` only, per the authorisation -- the sibling `Lock` arm has the identical gap and is registered separately as D104 above, not fixed here. | fixed and verified | **measured**, test written first and confirmed to fail pre-fix (TDD): Task 9's `test_a_nodebb_reply_whose_url_contains_post_still_falls_back_to_the_reply` seeds a `PostReply` at `/post/999` with no matching `Post` row, dispatches, and asserts it unlocks with exactly one SUCCESS row; pre-fix it failed with `replies_enabled` still `False` (neither `post` nor `post_reply` resolved, so Fix 3's guard logged `'Unlock: post not found'` instead); mutation-killed by reverting the fallback and re-observing the same failure |
| D109 | `process_inbox_request`, `Undo`/`ChooseAnswer` (routes.py:1872-1875 pre-fix) | **FIXED, this task, commit `4d604f18`.** `if isinstance(core_activity['object'], str): target_ap_id = core_activity['object'] else: target_ap_id = core_activity['object']['object']` -- the `isinstance(..., str)` branch was unreachable: control only reaches `ChooseAnswer` after `core_activity['object']['type'] == 'ChooseAnswer'` succeeded two lines above, and subscripting a string by `'type'` raises `TypeError` before that comparison can ever be reached with a string `core_activity['object']`. Same equivalent-mutant class as D95/D96 (5c). Fix: the conditional replaced with the direct assignment, `target_ap_id = core_activity['object']['object']`. No behaviour change. | fixed and verified | **measured**: this task's `test_a_string_inner_object_cannot_reach_choose_answer_at_all` (`tests/test_inbox_dispatch_undo_content.py`) sends a string inner `object` and asserts `dispatch()` raises `TypeError` -- proving the branch's own precondition can never be met; first identified by Task 7's report while writing `Undo`/`ChooseAnswer`'s own tests, deferred to this task by name |

### 4. The allocation ledger, updated

D1-D20 sub-project 2a, D21-D24 sub-project 2b, D25-D29 sub-project 2c,
D30-D33 sub-project 2c's whole-branch review, D34-D40 sub-project 3, D41-D46
sub-project 4, D47 sub-project 4's whole-branch review, D48 the follow-on
audit of `instance_allowed`/`instance_banned`, D49-D63 sub-project 5a,
D64-D77 sub-project 5b, D78-D79 5b's fix-wave review corrections, D80-D96
sub-project 5c, D97-D102 5c's six explicitly-authorised fixes, D103 5c's
fix-wave finding. **D104 this sub-project** (section 2 above): the sibling
`Lock` arm's identical missing `/post/`-to-`PostReply` fallback, registered
but not fixed (5c's territory). **D105-D109 this sub-project's five fixes**
(section 3 above): four explicitly authorised by the project owner --
D105-D106 `Undo`/`Lock`'s dead membership test and its `post`/`post_reply`
mixup (`3afc7abd`), D107 `Undo`/`Lock`'s misbound `else:` (`0905ba1a`), and
D109 `Undo`/`ChooseAnswer`'s
unreachable string branch (`4d604f18`); one, D108, authorised separately by
the controller as repair of the regression D105's own fix introduced --
the `/post/`-fallback loss (`9bfe5d5a`).

### 5. Three defects found but not registered until this fix wave -- D110-D112

Found during the original sub-project 5d work but never added to this
register, in violation of spec success criterion 5 (the register must carry
every defect found). Added here as part of the whole-branch review's fix
wave.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D110 | `process_inbox_request`, `Undo`/`Lock` (routes.py, pre-fix-wave) | **FIXED, this fix wave.** The descendant-subtree `UPDATE` used `db.session.execute` while the sibling `Lock` arm (5c's territory) uses `session.execute` for the identical statement. `patch_db_session` (`app/utils.py`) does not patch `db.session` inside a Flask request context, and `shared_inbox` calls `process_inbox_request` synchronously in-request when `current_app.debug`. In that mode the raw `UPDATE` would run on the Flask-scoped session while the following `session.commit()` commits the *task* session instead, discarding the descendants' write. Pre-existing since the `Undo`/`Lock` arm was written, but only became reachable once Fix 1 (D106) stopped the branch dying at `post.author` before ever reaching this line. Fix: `db.session.execute` changed to `session.execute`, matching the sibling `Lock` arm. Under `patch_db_session` (the harness's own test path) the two are identical, so no test behaviour changes; in a real request context the `UPDATE` and the following `session.commit()` now act on the same session. | fixed and verified | `tests/test_inbox_dispatch_undo_moderation.py`'s `test_unlocking_a_comment_re_enables_the_descendant_subtree` seeds a parent and child reply, both `replies_enabled = False`, and asserts both go `True` after an `Undo`/`Lock` on the parent -- the first test in this sub-project's own suite to assert the subtree write behaviourally rather than merely execute the statement |
| D111 | `process_inbox_request`, `Undo`/`Lock` (routes.py:1791) | **Not fixed, registered only -- out of scope for this wave.** `reason = core_activity['summary'] if 'summary' in core_activity else ''` reads the OUTER `Undo`'s `summary`, not the inner `Lock` object's. The sibling `Undo`/`Delete` sub-type correctly reads `core_activity['object']['summary']` (the inner object), and the non-`Undo` `Lock` arm correctly reads `core_activity['summary']` because there `core_activity` IS the `Lock` itself. For an `Undo`/`Lock`, the reason lives on the inner `Lock` object, one level down from where this line reads it -- a federated unlock's reason very likely never reaches the modlog. | not fixed, out of scope | **reading-level, not measured**: high-confidence by analogy with the `Undo`/`Delete` sibling's correct inner-object read, but Lemmy's exact `Undo`/`LockPost` wire payload was not confirmed from this repo, so whether real federated traffic actually nests the reason this way is inferred, not observed. No test in this suite passes a `summary` through an `Undo`/`Lock` activity at all, so the ternary's true arm (`'summary' in core_activity`) is never evaluated on either side -- full statement coverage of this line was reached without ever exercising the branch this defect lives in |
| D112 | `process_inbox_request`, `Undo` arm (routes.py:1676 onward) | **Not fixed, registered only -- out of scope for this wave.** An `Undo` whose `object` is a bare URI string, rather than a nested activity dict, raises an unhandled `TypeError` out of `process_inbox_request` (subscripting a string by `'type'` at `core_activity['object']['type']`). This is a legal ActivityPub shape and it is peer-triggerable -- nothing in the preamble or this arm validates `object`'s shape before dispatching on it. | not fixed, out of scope | **measured**: already pinned by `tests/test_inbox_dispatch_undo_content.py`'s `test_a_string_inner_object_cannot_reach_choose_answer_at_all`, which sends a string inner `object` and asserts `dispatch()` raises `TypeError` -- this is the entire factual basis for D109 (the `ChooseAnswer` branch's unreachable `isinstance(..., str)` check), but the underlying defect -- an unhandled crash on a legal, peer-triggerable shape -- was itself never registered, only its downstream consequence (D109) was |

**Next free number: D113.**



## Sub-project 5e: the `Create`/`Update` arm, and the six statements left elsewhere in `process_inbox_request`

`docs/superpowers/specs/2026-09-01-coverage-inbox-create-update-5e-design.md`,
on branch `blentz`. Ten tasks brought `process_inbox_request`'s `Create`/`Update`
arm and six neighbouring statements elsewhere in the function toward full
statement coverage. `app/activitypub/routes.py` measures **62.5873% blended**
(1158/1823 statements, 545/898 branches) after this sub-project, up from
59.4465% before it (full suite: 3049 passed, 3 skipped). Tests live mainly in
`tests/test_inbox_dispatch_create_update.py` (28 test functions, several
parametrized, added across Tasks 2-9), plus one test each added to
`tests/test_inbox_dispatch_votes.py` and `tests/test_inbox_dispatch_preamble.py`
(Task 10, the six neighbouring statements: the `PollVote`/`ChooseAnswer`
delegate dispatch and the Community-actor Group-fallthrough). `process_inbox_request`
now has **exactly one uncovered statement** in the whole function --
`routes.py:860`, a debug `pass` (D117 below).

Like 5b, 5c and 5d before it, this sub-project carried a **narrow, explicitly
authorised exception** to the campaign's report-don't-fix rule: Task 9 was
separately authorised to fix the two defects Task 8 pinned as crash and
silence surfaces, both confined to the Create/Update arm. `git diff --stat app/`
is non-empty for exactly Task 9 and its post-review correction round -- four
commits in total: `15b00579` and `426dc4d3` (the two original fixes), then
`a350674d` (hoisting the first fix's guard to close a fourth crash path the
reviewer found) and `cf97f307` (a test-docstring-only correction, carrying no
production diff). Every other task's `git diff --stat app/` is empty.

### 1. Two defects fixed, under explicit authorisation -- D113-D114

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D113 | `process_inbox_request`, `Create`/`Update` arm (routes.py:1193-1243, as reached via a feed-Announce) | **FIXED, commit `15b00579`, guard hoisted by `a350674d`.** When an `Announce`'s outer actor resolves to a *feed* (not a community or user), the preamble (routes.py:840-932, sub-project 5a's territory) sets `announced = True` while leaving both `user` and `community` `None` -- `community` is never reassigned past its routes.py:862 `None`, and `user` is explicitly set `None` at routes.py:924 because the `if not feed:` branch that would otherwise resolve it never runs. The arm's own resolution chain, `if not announced and not community:` (routes.py:1244), is then skipped entirely because `announced` is `True`. Four downstream consequences, all genuine crashes established by reading rather than assumed (Task 9's post-review addendum corrected an earlier claim that one of the four was merely a silent pass-through): `process_chat`'s `sender = session.query(User).get(user.id)` (routes.py:2530), `Group`'s `community.is_local()` (routes.py:1274 branch), the poll vote's `poll_data.vote_for_choice(choice.id, user.id)` (routes.py:1228), and `process_new_content`'s first executable line, `if user.user_name == 'rimu':` (routes.py:2297). Fix: a guard, `if user is None and community is None:` (routes.py:1202-1214), now refuses with `log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Cannot process Create/Update: no user or community resolved')` before any of the four is reached. The guard's first landing (`15b00579`) placed it after the `ChatMessage` check, leaving `process_chat`'s crash live; post-review, `a350674d` moved it above that check, closing the fourth path. Scope note: PieFed's own feed Announces only ever wrap `Add`/`Remove` of a `Group` (`app/shared/feed.py`), never a `Create`/`Update`, so this guard refuses nothing this codebase itself emits -- the exposure is to other federating software that announces differently. | fixed and verified | **measured**: Task 8's three tests (`tests/test_inbox_dispatch_create_update.py`) pinned the pre-fix crashes directly (`pytest.raises(AttributeError)` for the Group and poll-vote paths; a doubled `process_new_content` call receiving two `None`s for the Page path); Task 9 inverted all three and added a fourth, `test_a_feed_announced_chat_message_is_refused_instead_of_crashing_on_a_none_user`, which failed with a genuine `AssertionError` (`process_chat` called with `user=None`) against the guard's first, too-late placement and passed only once `a350674d` moved it -- a real mutation kill, not a doubled-call artefact |
| D114 | `process_inbox_request`, `Create`/`Update` arm, poll-vote block (routes.py:1219-1243) | **FIXED, commit `426dc4d3`.** Three of the poll-vote block's four outcomes -- post not found, poll not found, choice not found -- fell to the block's unconditional `return` (routes.py:1243) having called `log_incoming_ap` zero times; only the full-success path logged anything, so a poll vote a peer sent that silently failed to land left no trace in `ActivityPubLog` to explain why. Fix: each of the three now logs a distinct `APLOG_IGNORED` message before the same `return` -- `'Poll vote for an unknown post'` (routes.py:1240-1242), `'Poll vote for a post with no poll'` (routes.py:1234-1236), `'Poll vote for an unknown choice'` (routes.py:1237-1239). The `return` itself is unchanged (see D116 below). | fixed and verified | **measured**: Task 4's three tests pinned the pre-fix silence directly (`ActivityPubLog.query.count() == 0` after dispatch, for all three outcomes); Task 9 inverted them to assert one `APLOG_IGNORED` row each with the matching reason string, and mutation-killed each log independently (removing the "unknown post" log failed only its matching test: `1 failed, 34 passed`) |

### 2. Four defects registered, not fixed -- D115-D118

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D115 | `process_inbox_request` preamble (routes.py:840-932, sub-project 5a's territory) | **Not fixed, registered only -- out of scope for this sub-project.** D113's guard only mitigates the root cause locally: the preamble leaves `user` and `community` both `None` for *any* Announce whose outer actor resolves to a feed, not just when the inner object is a Create/Update. Every other arm reached after the preamble (Like, Dislike, Delete, Follow-adjacent activities, etc.) is potentially exposed to the same unguarded assumption and has not been audited here -- this sub-project was confined to the Create/Update arm, per its brief. | not fixed, out of scope | **reading-level**: Task 9's report traces the preamble's assignment of `user = None` (routes.py:924, gated on `if not feed:` being False) and `community` staying at its routes.py:862 `None`, and states the out-of-scope reasoning directly; no test in this sub-project dispatches a feed-Announce through any arm other than Create/Update, so whether another arm actually crashes on the same `None`s is inferred by the same reasoning that proved Create/Update's four crashes, not observed |
| D116 | `process_inbox_request`, `Create`/`Update` arm, poll-vote block (routes.py:1243) | **Not fixed, deliberately -- a product decision, not a defect this sub-project was positioned to resolve.** The poll-vote block's trailing `return` is unconditional: once an activity matches the poll-vote shape (routes.py:1219-1221), it never falls through to `process_new_content` or `process_chat`, even when nothing about it actually resolved (D114's three now-logged outcomes). Whether a poll-shaped activity that fails to resolve as a poll vote should instead be tried as ordinary content is a behavioural choice outside this sub-project's authorisation, so the `return` is left exactly where it was -- D114 only made its three silent branches audible. | not fixed, product decision | **reading-level**: confirmed directly from routes.py:1219-1243 -- the `return` sits at the same indentation as the `if post_being_replied_to:` it follows, so it is reached regardless of which of the four outcomes (success, unknown post, no poll, unknown choice) occurred; Task 9's report names this explicitly as an unaddressed, separate product decision |
| D117 | `process_inbox_request` preamble (routes.py:859-860) | **Not fixed, deliberately -- and now the single remaining uncovered statement in the whole function.** `if actor_id and actor_id.startswith('https://s.rimu.geek.nz'):` / `pass`, with the inline comment *"just here to set breakpoints on, during testing. remove before commit"*. Debug scaffolding reachable only from one named personal domain -- not fixed and not covered, on the reasoning that writing a test to cover a `pass` its own author marked for deletion would entrench code that should not exist rather than document real behaviour. Recommend deletion: doing so removes both the statement and its branch cleanly, and would take `process_inbox_request` to full statement coverage. | not fixed, recommend deletion | **measured**: the controller's own coverage measurement after this sub-project shows `process_inbox_request` at exactly one uncovered statement, `routes.py:860`; the line and its comment were re-read directly against current HEAD for this register entry (routes.py:859-860), confirming both the line numbers and the comment text quoted above |
| D118 | `process_new_content` (routes.py:2296-2298), the delegate the Create/Update arm calls for ordinary new content | **Not fixed, registered only -- same class of artefact as D117, one function away.** `if user.user_name == 'rimu':` / `pass`, no comment. The same debug-scaffolding pattern as D117 (a no-op `pass` gated on identifying one specific person), in the delegate this sub-project's arm calls for `Page`/`Article`/etc. content. Recommend deletion. | not fixed, recommend deletion | **reading-level**: read directly from routes.py:2296-2298 (`def process_new_content(user, community, store_ap_json, request_json, announced):` / `if user.user_name == 'rimu':` / `pass`); no test in this sub-project exercises the `True` arm of this branch (doing so would require a `User` literally named `rimu`), so this is derived from reading the delegate's source, not from a dispatched activity |

### 3. Two more defects found by this fix wave's whole-branch review -- D119-D120

Both live in the exact block D114 documents (the Note-shaped poll-vote path,
routes.py:1219-1243) and were found by re-reading that block against its
sibling, `process_poll_vote` (routes.py:2461-2485), and against local voting
(`vote_for_poll`, `app/shared/post.py:1146-1174`). Registered here, **not
fixed** -- out of this sub-project's authorised scope, which was the two
fixes already made under D113/D114.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D119 | `process_inbox_request`, `Create`/`Update` arm, poll-vote block (routes.py:1219-1243) | **Not fixed, registered only -- out of scope.** The Note-shaped poll-vote path performs no banned-instance check anywhere in its block. Its sibling federated path, `process_poll_vote` (routes.py:2461-2485), guards the vote with `if not instance_banned(user.instance.domain):` (routes.py:2473) before calling `vote_for_choice`; the Note-shaped path calls `poll_data.vote_for_choice(choice.id, user.id)` (routes.py:1228) with no equivalent check anywhere in the block. Consequence: a user on a banned instance can land a poll vote by sending a `Create` of a Lemmy-shaped `Note`, but not by sending a `PollVote` -- two federated routes to the same effect, one enforcing the ban and one not. | not fixed, out of scope | **reading-level**: confirmed by reading both blocks side by side -- routes.py:1219-1243 has no `instance_banned` call anywhere in its body, while routes.py:2473 (`process_poll_vote`) does; no test in this sub-project sends a banned-instance user through the Note-shaped path to observe the vote land, so this is derived from reading, not from a dispatched activity |
| D120 | `process_inbox_request`, `Create`/`Update` arm, poll-vote block (routes.py:1228-1229), its sibling `process_poll_vote` (routes.py:2478-2479), and `Poll.vote_for_choice` (`app/models.py:3758-3767`) | **Not fixed, registered only -- out of scope.** Neither federated poll-vote path enforces `Poll.mode`. Local voting does: `vote_for_poll` (`app/shared/post.py:1158-1164`) gates a `mode='single'` poll on `poll.has_voted(user.id)` before calling `vote_for_choice`, so a local single-mode voter can hold at most one vote across the whole poll. `Poll.vote_for_choice` itself (`app/models.py:3758-3767`) dedupes only on the `(user_id, choice_id)` pair -- its `existing_vote` query filters on `choice_id`, not on the poll as a whole -- with no poll-mode awareness at all, so a remote user can accumulate one vote on *every* choice of a `mode='single'` poll by sending several Notes (or several `PollVote`s), each landing because each targets a different `choice_id`. Folded into this row: both federated paths also log their SUCCESS row unconditionally -- the Note-shaped block's `log_incoming_ap(id, APLOG_CREATE, APLOG_SUCCESS, saved_json)` (routes.py:1229) and `process_poll_vote`'s equivalent (routes.py:2479) both fire immediately after calling `vote_for_choice`, with no check on whether that call actually inserted a new `PollChoiceVote` row or silently no-oped against an existing `(user_id, choice_id)` pair (`app/models.py:3761`'s `if not existing_vote:` guard) -- so a duplicate vote from the same peer is logged as a success that recorded nothing. | not fixed, out of scope | **reading-level**: confirmed by reading `app/shared/post.py:1158-1164` (the local single-mode gate) against routes.py:2473-2479 and routes.py:1219-1243 (both federated paths, neither reads `poll.mode`), and `app/models.py:3758-3767` (`vote_for_choice`'s dedupe key is `(user_id, choice_id)`, not poll-scoped); no test in this sub-project seeds a `mode='single'` poll and sends votes for two different choices from the same remote user, so the accumulation itself is derived from reading, not observed |

### 4. A coverage-tool artefact worth recording, not a defect: the arm's commonest federated path is untested by this file alone -- and by more than that file

Run in isolation, `tests/test_inbox_dispatch_create_update.py` leaves the
branch at routes.py:1244 (`if not announced and not community:`) with its
False arm -- the skip of the community-resolution block -- uncovered
(`missing_branches: [[1244, 1256]]`, measured directly against this file
alone). That much matches the whole-branch review's observation. But the
attribution does not: the False arm is **not** closed by
`tests/test_inbox_dispatch_announce.py` -- measured directly, running the
two files together still reports `[[1244, 1256]]` missing. It is closed
only once the full `tests/test_inbox_dispatch_*.py` family runs together,
and by a single test in a third file:
`test_update_group_from_a_group_actor_is_processed_as_a_community_update`
(`tests/test_inbox_dispatch_preamble.py`), which dispatches a direct
(non-`Announce`) `Update`/`Group` from a Community actor -- `community`
arrives pre-resolved via routes.py:881's `community = actor`, `announced`
is `False`, and the same False arm at 1244 is taken because `community` is
truthy, not because `announced` is `True`.

That means the scenario the review actually had in mind -- an ordinary
`Announce` of a `Create`/`Update`, wrapped by a real community, with
`announced` `True` and `community` pre-resolved by the preamble's own
Announce handling (routes.py:861) -- is not exercised by any file in this
suite. Branch coverage reports the arc closed because a different,
unrelated scenario happens to leave the same condition False; the
literal "commonest federated path" this review named remains untested.
No test anywhere in `tests/test_inbox_dispatch_*.py` dispatches an
`Announce` from a community actor whose inner object is `Create`/`Update`
-- confirmed by grep across the whole family for a literal `'Create'`
(found only in this sub-project's own file) or `'Update'` paired with
`activity_type='Announce'` (found in neither).

Not filed as a defect: this is a test-suite gap, not a behaviour of
`app/`. Not written as a docstring correction either, since no existing
docstring makes the wrong claim -- the misattribution originated in this
fix wave's own brief, not in shipped test text. Recorded here instead,
as the honest register entry for what the coverage tool's "arm is fully
covered" framing (section header above, "process_inbox_request now has
exactly one uncovered statement") does not surface: statement and branch
coverage both say nothing about *which* scenario satisfied a given arc,
and here the scenario that did is not the one anyone should rely on as
having verified the ordinary case.

**Next free number: D121.**

## Sub-project 6: `process_chat`, the private-message acceptance policy

`docs/superpowers/specs/2026-09-01-coverage-inbox-chat-6-design.md` and
`docs/superpowers/plans/2026-09-01-coverage-inbox-chat-6.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-01-coverage-inbox-chat-6/`, not committed), on
branch `blentz`. Ten tasks brought `process_chat` -- the
ActivityPub inbox's private-message acceptance policy,
`app/activitypub/routes.py:2527-2630` -- from 4.1% statement coverage to
**zero uncovered statements**, and fixed three defects across two commits.
Task 1 added the `make_conversation` factory the later tasks needed; Tasks
2-9 wrote the coverage and, under separate authorisation, Task 9 fixed the
three defects Task 8 had pinned (its own report calls them Defect 1, 2 and
3); this task (10) closes the sub-project out with the findings register,
the test-harness log, and the coverage floor.
`app/activitypub/routes.py` measures **66.4223% blended** (1234/1828
statements, 578/900 branches) after this sub-project, up from 62.5873%
before it (full suite: 3077 passed, 3 skipped). Tests live in
`tests/test_inbox_dispatch_chat.py` (25 test functions, two parametrized
into two cases each, so 27 test invocations, added across Tasks 2-9).

Like 5b, 5c, 5d and 5e before it, this sub-project carried a **narrow,
explicitly authorised exception** to the campaign's report-don't-fix rule:
Task 9 was separately authorised to fix the three defects Task 8 pinned,
all confined to `process_chat`. `git diff --stat app/` is non-empty for
exactly Task 9's two commits, `5783beb8` and `d0c8d13f`. Every other task's
`git diff --stat app/` is empty.

### 1. Three defects fixed, under explicit authorisation -- D121-D123

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D121 | `process_chat` (`app/activitypub/routes.py:2565-2568`) | **FIXED, commit `5783beb8`.** `core_activity['object']['content']` was read with no membership check anywhere above it, in the branch reached once every recipient-policy check -- sender-too-new, blocked user/instance, and all three `accept_private_messages` outcomes (`routes.py:2550-2564`) -- has already declined to refuse the message. A peer-supplied `ChatMessage` missing `content` raised an uncaught `KeyError: 'content'` out of a Celery task instead of being refused like every sibling branch in this arm. Fix: `if 'content' not in core_activity['object']:` (`routes.py:2566-2568`), logging `'ChatMessage has no content'` and returning `True` ("handled") before the field is first dereferenced -- placed at the top of the `else:` branch (`routes.py:2565`) that begins only once every recipient-policy refusal above it has failed to fire. **The blocked-phrase filter's separate falsy-content skip is unchanged by this fix**: `if core_activity['object']['content']:` (`routes.py:2573`) still tests truthiness, not membership, so a `ChatMessage` with `content: ''` (present but empty) still passes this new guard -- the key exists -- and still skips the blocked-phrase loop exactly as it did before Fix A. The new guard closes only the absent-key crash; the pre-existing falsy-value filter-skip is a distinct, unrelated behaviour and was deliberately left alone. | fixed and verified | measured, for the crash and its fix: Task 8 pinned the pre-fix `KeyError: 'content'` directly; Task 9 inverted the test to assert a clean refusal (`log.result == 'failure'`, `'content'` in `log.exception_message`, zero `ChatMessage` rows) and mutation-killed the fix with a literal revert (`git apply -R` on the fix's diff), reproducing the original `KeyError` -- a genuine exception kill, not a `respx.models.AllMockedAssertionError` infrastructure kill. reading-level, for the falsy-content claim: confirmed directly against `routes.py:2573` and Task 9's own report, which states no test in the file exercises `content: ''` either way -- the claim is about what did NOT change, so nothing new was asserted to prove it |
| D122 | `process_chat` (`app/activitypub/routes.py:2569-2571`) | **FIXED, commit `5783beb8`** (same commit as D121, a distinct guard). `core_activity['object']['id']` was read with no membership check anywhere above it -- reached only once `content` has already been confirmed present (D121's guard) and, if truthy, has cleared the blocked-phrase filter. A peer-supplied `ChatMessage` with `content` present but `id` absent raised an uncaught `KeyError: 'id'` out of a Celery task. Fix: `if 'id' not in core_activity['object']:` (`routes.py:2569-2571`), logging `'ChatMessage has no id'` and returning `True` ("handled"), placed immediately after D121's `content` guard so the two failure modes stay independently reachable -- a message missing only `content` never reaches this check, and one missing only `id` passes D121's check cleanly first. | fixed and verified | measured: Task 8 pinned the pre-fix `KeyError: 'id'` directly and traced that it fired only once the `content` check, and (for the empty `blocked_phrases()` default in this suite) the phrase loop, had both already passed cleanly -- proving this crash is independent of D121's, not a symptom of it; Task 9 inverted the test to assert a clean refusal (`log.result == 'failure'`, `'id'` in `log.exception_message`, `'content'` NOT in it, zero `ChatMessage` rows) and mutation-killed the fix with a literal revert, reproducing the original `KeyError` -- a genuine exception kill |
| D123 | `process_chat` (`app/activitypub/routes.py:2548-2549`; the removed inner guard sat at `routes.py:2613` immediately before this fix) | **FIXED (equivalent-mutant removal), commit `d0c8d13f`.** The `publish_sse_event` call and `Notification` write sat under a second `if recipient.is_local():` check, nested inside a block already entered via `if recipient and recipient.is_local():` (`routes.py:2548`). `recipient` is reassigned immediately after, at `routes.py:2549`, to a row fetched by id from the same session -- that line's own comment explains this is because `find_actor_or_create_cached` was "giving me a user from the wrong DB session, causing an exception later on" -- and nothing between that reassignment and the inner check ever writes `recipient.ap_id` -- one of the two fields `User.is_local()` reads (`app/models.py:1242-1243`: `self.ap_id is None or self.ap_profile_id.startswith(...)`); the other, `recipient.ap_profile_id`, is equally unwritten anywhere in that span -- or otherwise changes which row `recipient` points at, so the inner check could never observe a different truth value than the outer one already established. A genuine equivalent mutant, same class as D95, D96 and D103. Fix: the inner guard was removed and its body (the `publish_sse_event` call and the `Notification` write, now unconditional starting at `routes.py:2613`) de-indented one level. | fixed and verified | measured: Task 8's `test_the_inner_is_local_check_can_never_be_false` passes identically before and after the removal -- that equality, not a pass/fail flip, is the intended proof of deadness for a true equivalent mutant. Task 9 additionally proved the test is not a tautology by temporarily forcing the guard's body unreachable pre-fix (`if False:`), which failed the same test for a real reason (assertions on `publish_sse_event` call count, `Notification` count, and `unread_notifications` never firing); a literal revert of the fix afterward produced no kill, which is the expected, honestly-reported result for an equivalent mutant, not a weak-test finding |

### 2. Three items registered, not fixed -- D124-D126

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D124 | `process_chat` (`app/activitypub/routes.py:2562`) | **Not fixed, registered only.** `elif recipient.accept_private_messages == 2 and not sender.instance.trusted:` reads `sender.instance.trusted` with no check that `sender.instance` -- the `User.instance` relationship (`app/models.py:1052`), joined on the nullable `instance_id` column (`app/models.py:1017`, a plain `db.ForeignKey('instance.id')` with no `nullable=False`) -- is non-null. Reached whenever the recipient's `accept_private_messages` is `2` ("Trusted instances"). A `sender` whose `instance_id` were `NULL` would raise `AttributeError: 'NoneType' object has no attribute 'trusted'` here. | not fixed, registered only | reading-level for the null-dereference exposure itself (`app/models.py:1017,1052` confirm the nullable FK and the relationship it backs); the tests establish nothing about whether this state is reachable, in either direction -- `seed_chat_pair` (`tests/test_inbox_dispatch_chat.py:120-155`) always calls `make_instance(host)` and passes the resulting row into `make_user`, so every sender that reaches this line in this suite has a real, non-null `Instance` row; no test seeds a sender with a null `instance_id` and dispatches it through this branch, so whether a production code path can produce one is neither proven nor disproven here |
| D125 | `process_chat` (`app/activitypub/routes.py:2550`) | **Not fixed, registered only -- harmless today.** The new-account exemption, `if sender.created_very_recently() and user.ap_domain != 'fediseer.com':`, reads `user.ap_domain` in its second conjunct where its first conjunct, and every other line in the function, reads `sender`. `sender` (`routes.py:2530`, `sender = session.query(User).get(user.id)`) is the same database row as the `user` parameter, re-fetched into the function's own `session` -- the identical session-identity concern the code's own comment two lines below describes for `recipient` (`routes.py:2549`: "for some reason find_actor_or_create_cached was giving me a user from the wrong DB session, causing an exception later on"). Because `sender` and `user` are the same row, `sender.ap_domain` and `user.ap_domain` return the same value today, so reading `user.ap_domain` here is harmless as written. It is recorded because the whole reason `sender` exists as a separate name is to keep the "same row, re-fetched into this session" distinction straight, and this line is the one place that distinction is not observed. | not fixed, harmless today | reading-level: `routes.py:2530` and `2549-2550` read directly; no test in the suite could distinguish `user.ap_domain` from `sender.ap_domain` since they are never diverged -- Task 4's `test_a_brand_new_sender_from_fediseer_is_exempt` sets `sender.ap_domain = 'fediseer.com'` on the very object `session.query(User).get(user.id)` returns, so the two names are aliases of one row throughout this file |
| D126 | `process_chat` (`app/activitypub/routes.py:2556-2565`) | **Not a defect -- judged unspecified behaviour by both the implementer and this register.** The `accept_private_messages` chain checks `None`, `0` (`routes.py:2556`), `1` (`routes.py:2559`) and `2` (`routes.py:2562`) explicitly; any other value -- including the documented `3` ("All instances", the column's own default, `app/models.py:1035`) but equally any undocumented value such as `4` or `-1` -- falls through to the accepting `else:` (`routes.py:2565`) with no final `else` refusal for unexpected values. `accept_private_messages` is written from a fixed set of UI values `0`-`3` by the settings form's `SelectField(choices=accept_from, coerce=int)` (`app/user/forms.py:123-129`) and the account API's string-to-int mapping (`app/api/alpha/utils/user.py:554-562`), plus one hardcoded `0` for the auto-created feed-bot account (`app/cli.py:1200`, harmless -- a bot that never accepts PMs). It is also written with **no validation at all** from a remote actor document at `app/activitypub/util.py:723` (`user.accept_private_messages = activity_json['acceptPrivateMessages'] if 'acceptPrivateMessages' in activity_json else 3`, updating an existing remote user) and `:1222` (the same expression, constructing a new remote user) -- a peer server's `acceptPrivateMessages` field passes straight through with any JSON value it likes, `int` or not, `0`-`3` or not. The conclusion still holds, but not for the reason "every writer is constrained": both unvalidated writers touch only a **remote** user's row (they run inside actor-fetch/actor-update code building or refreshing a `User` for a peer server's account), and `process_chat`'s `recipient` is, by the outer `recipient.is_local()` guard (`routes.py:2548`) that gates this whole chain, always **local**. A remote actor can set its own `accept_private_messages` to anything; it can never set a local recipient's. So the `else` branch's true boundary (">= anything unmatched above" vs. "== 3 specifically") is unproven by any test but not reachable through any first-party path either -- "first-party path" here meaning any path that can write a *local* user's column, which is what the row's judgment actually rests on. | not a defect, unspecified behaviour | reading-level: `routes.py:2556-2565` read directly; Task 5's report states no test distinguishes `3` from an untested out-of-range value, and its own judgment (concurred with here) is that this is unspecified rather than defective, since every first-party writer of the column restricts it to `0`-`3` |

### 3. Five more defects found by this fix wave's whole-branch review -- D127-D131

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D127 | `process_chat` (`app/activitypub/routes.py:2529`) | **Not fixed, registered only -- out of scope; the same pattern is dispatcher-wide.** `id = core_activity['id']` is the first line of `process_chat`, read with no membership check. The preamble validates the *outer* activity's `id` (`routes.py:650`, `shared_inbox`'s `if not 'id' in request_json or ...`), but the Announce unwrap (`routes.py:929`, `core_activity = request_json['object']`) never re-validates the *inner* object it hands to `core_activity`, and nothing between the unwrap and the `Create`/`Update` arm's `ChatMessage` branch (`routes.py:1215-1216`) checks `core_activity['id']` either. An `Announce{Create{ChatMessage}}` whose inner `Create` activity has no `id` reaches `process_chat` and raises an uncaught `KeyError: 'id'` out of a Celery task -- the identical failure mode D122 fixed, but for the *activity's* own `id` one level up, not the `ChatMessage` object's `id` D122 guards. The same unguarded pattern recurs dispatcher-wide: the `Follow` arm reads `follow_id = core_activity['id']` (`routes.py:937`) immediately after the same unwrap, with the same absent re-validation. Registered rather than fixed because closing it means auditing every arm's `id` read after an Announce unwrap, not a single-function guard like D121/D122 -- out of this sub-project's scope. | not fixed, registered only | reading-level: `routes.py:650,929,937,1215-1216,2529` read directly; no test in this suite or the wider Create/Update-arm suite dispatches an `Announce{Create{ChatMessage}}` with an id-less inner activity, so the crash is derived from reading, not observed |
| D128 | `process_chat` (`app/activitypub/routes.py:2566-2568`) | **Not fixed, registered only.** The Task 9 guard `if 'content' not in core_activity['object']:` checks membership only, not type. `core_activity['object']['content']` being *present* but non-string -- for example `{'content': {'x': 1}}` -- passes this guard, passes the truthiness check at `routes.py:2573` (a dict is truthy), and is then handed to `html_to_text()` (`app/utils.py:1231-1235`, `BeautifulSoup(html, 'html.parser')`) at `routes.py:2594` (and stored raw as `body_html` at `routes.py:2593`); `BeautifulSoup` does not accept a `dict` as markup and raises. Contrast the same function's handling of `to` (`routes.py:2534-2542`), which checks `isinstance(..., str)` and `isinstance(..., list)` for both JSON-LD shapes before using the value -- `content` and `id` get a presence check only, not a shape check. | not fixed, registered only | reading-level: `routes.py:2566-2568,2573,2593-2594` and `app/utils.py:1231-1235` read directly; no test in this suite sends a dict-valued `content`, so the crash is derived from reading `BeautifulSoup`'s accepted input types, not observed |
| D129 | `process_chat` (`app/activitypub/routes.py:2569-2571`) | **Not fixed, registered only.** The sibling guard, `if 'id' not in core_activity['object']:`, has the same gap as D128: `core_activity['object']['id']` being present but non-string -- for example `{'id': ['a', 'b']}` -- passes the guard and flows into `session.query(ChatMessage).filter_by(ap_id=core_activity['object']['id'])` (`routes.py:2589`) and, on the create path, into the `ChatMessage(..., ap_id=core_activity['object']['id'])` constructor (`routes.py:2596`) -- a list handed to an equality filter and to a column expecting a scalar string, rather than the clean, logged refusal every other malformed-input path in this function produces. Same class of gap as D128, on the sibling field. | not fixed, registered only | reading-level: `routes.py:2569-2571,2589,2596` read directly; no test in this suite sends a list-valued `id`, so the downstream failure mode (a SQLAlchemy/DB-level error rather than a clean refusal) is derived from reading, not observed |
| D130 | `process_chat`, both call sites (`app/activitypub/routes.py:1215-1216` and `:1246-1249`) | **Not fixed, registered only -- a behaviour change introduced by D121's fix, not itself a defect needing its own code change.** `process_chat` has two call sites: the dedicated `ChatMessage` branch (`routes.py:1215-1216`), which discards the return value entirely, and the `Create`/`Update` arm's fallback (`routes.py:1246-1249`, `if process_chat(...): return`), reached only when `object['type']` is something else (`Page`, `Note`, `Article`, ...) and `find_community` resolved nothing. Because the first call site discards the return value, D121's `content`-guard `True` return is only ever observable at the second, non-`ChatMessage` call site. A well-formed link-style `Page` -- no `content` key at all, which link posts legitimately lack -- addressed (`to`) to a local user with no resolvable community now reaches D121's guard, is logged as a `'ChatMessage has no content'` FAILURE (a message that is actively misleading for a `Page`), and `process_chat` returns `True`, so the arm returns immediately and the `Page` is silently dropped. Before Fix A, the identical input reached the unguarded `core_activity['object']['content']` read in the blocked-phrase check (pre-fix `routes.py:2567`) and raised an uncaught `KeyError` out of the Celery task instead -- a loud failure, visible as a task exception, rather than a quiet logged one. Nothing was delivered as a post in either world, so this is not a regression in outcome, but the *visibility* of the failure changed from loud to quiet. No test in this file exercises a content-less `Page` through the fallback path: both tests reaching the 1247 call site build their `Page` with `content: 'hello'` present. | not fixed, registered only | reading-level: `routes.py:1215-1216,1246-1249,2566-2568` read directly; `test_a_handled_chat_stops_the_arm_from_treating_it_as_content` (`tests/test_inbox_dispatch_chat.py:317-338`) and `test_an_unhandled_chat_lets_the_arm_continue_to_the_domain_check` (`tests/test_inbox_dispatch_chat.py:341-358`), the file's only tests reaching the 1247 call site, both supply `content`, so this specific input shape is untested in either direction |
| D131 | `process_chat` (`app/activitypub/routes.py:2573`) | **Not fixed, registered only -- previously undernumbered.** `if core_activity['object']['content']:` tests truthiness, not membership, before running the blocked-phrase loop (`routes.py:2572-2578`). A `ChatMessage` whose `content` is present but falsy -- not only `''` (already noted in D121's evidence, harmless there since an empty string has no phrase to match) but any other JSON-falsy, non-string value the D128 gap lets through unconverted, such as `content: 0` or `content: false` -- skips the blocked-phrase filter entirely and proceeds toward storage. This behaviour was described only in D121's prose ("the blocked-phrase filter's separate falsy-content skip is unchanged by this fix") with no number of its own, making it uncitable independent of D121's row; this row gives it one. | not fixed, registered only | reading-level: `routes.py:2572-2578` read directly; no test in this suite sends a falsy non-string `content`, and `test_a_message_containing_no_blocked_phrase_is_delivered` (`tests/test_inbox_dispatch_chat.py:381-398`) covers only truthy content that matches no phrase, not falsy content that skips the check structurally |

**Next free number: D132.**

## Sub-project 7: `process_new_content`, the federated post/reply creation-and-edit delegate

`docs/superpowers/specs/2026-09-02-coverage-new-content-7-design.md` and
`docs/superpowers/plans/2026-09-02-coverage-new-content-7.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-02-coverage-new-content-7/`, not committed), on
branch `blentz`. Ten tasks brought `process_new_content` -- the ActivityPub
inbox's federated post/reply creation-and-edit delegate,
`app/activitypub/routes.py:2296-2417` -- to full statement coverage but for
one statement (`:2298`, already D118, deliberately left uncovered), and
fixed four defects across four commits, all under this sub-project's own
bounded, explicit authorisation; this task (10) closes the sub-project out
with the findings register, the test-harness log, and the coverage floor.
`app/activitypub/routes.py` measures **71.7423% blended** (1333/1832
statements, 627/900 branches) after this sub-project, up from 66.4223%
before it (full suite: 3106 passed, 3 skipped, 6 subtests passed, 168.49s).
Tests live in `tests/test_inbox_dispatch_new_content.py` (29 test
functions, none parametrized, added across Tasks 1-9).

Like 5b-5e and 6 before it, this sub-project carried a **narrow, explicitly
authorised exception** to the campaign's report-don't-fix rule: Task 9 was
separately authorised to fix the four defects Task 8's pin and the
whole-branch review had established, all confined to `process_new_content`.
`git diff --stat app/` is non-empty for exactly Task 9's four fix commits
(`62e9e928`, `22914a59`, `1ffd75b5`, `e092ff40`); its fix-round commit
(`26d764ed`) is test-only. Every other task's `git diff --stat app/` is
empty.

### 1. Four defects fixed, test-first with a mutation-proved test -- D132-D135

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D132 | `process_new_content` (`app/activitypub/routes.py:2372`) | **FIXED, commit `62e9e928`.** The reply half's edit-permission check had only two disjuncts (`user.id == reply.user_id or reply.community.is_moderator(user)`) where the post half's equivalent, `app/activitypub/routes.py:2328` (`user.id == post.user_id or post.community.is_moderator(user) or post.community.is_instance_admin(user)`), has three -- so an instance admin could edit a federated post but was refused editing a federated reply, falling into the `else:` at `app/activitypub/routes.py:2381-2383` that logs `'Edit attempt denied'`. Fix: added `or reply.community.is_instance_admin(user)`, so the reply guard at `:2372` now reads identically in shape to the post guard at `:2328`. | fixed and verified | measured: Task 8's `test_an_instance_admin_cannot_edit_a_reply` pinned the pre-fix refusal (`calls['update_post_reply_from_activity'] == []`, `log.exception_message == 'Edit attempt denied'`); Task 9 inverted it to `test_an_instance_admin_can_edit_a_reply` (`tests/test_inbox_dispatch_new_content.py:627`), asserting `len(calls['update_post_reply_from_activity']) == 1` and `log.result == 'success'`, and mutation-killed the fix by removing the added disjunct (`assert 0 == 1`). The inversion itself vacated the reply half's `else:` refusal branch -- every remaining reply-side test now passed the three-disjunct guard -- a real regression Task 9's own review caught and repaired in the same fix round with `test_an_unrelated_user_cannot_edit_a_reply` (`tests/test_inbox_dispatch_new_content.py:686`), whose own mutation kill (guard forced to `if True:`) is the sole cover for that branch ("1 failed, 28 passed") |
| D133 | `process_new_content` (`app/activitypub/routes.py:2378-2379`) | **FIXED, commit `22914a59`.** Reached once the outer edit-permission check passes (`:2372`) but the editor fails `can_create_post_reply(user, community)` (`:2373`): the pre-fix code had no `else` on that inner `if`, so control fell straight through to the shared `return` two lines below (today, after the fix, that `return` sits at `:2380`, reached from either branch of the new `else`) with no `log_incoming_ap` call on any path -- an edit refusal recorded nowhere. Fix: added an `else:` calling `log_incoming_ap(id, APLOG_UPDATE, APLOG_FAILURE, saved_json, 'User cannot create reply in Community')` (`app/activitypub/routes.py:2378-2379`) -- the identical message the reply-creation branch already uses for the same refused predicate (`:2416`), distinguished from it only by `activity_type` (`'Update'` here, since this branch requires an existing `reply` row, versus `'Create'` there). | fixed and verified | measured: Task 8's `test_a_permitted_editor_who_cannot_reply_is_dropped_silently` pinned the pre-fix silence (`ActivityPubLog.query.count() == 0`); Task 9 inverted it to `test_a_permitted_editor_who_cannot_reply_is_logged` (`tests/test_inbox_dispatch_new_content.py:596`), asserting one row with `log.result == 'failure'`, `log.activity_type == 'Update'`, `log.exception_message == 'User cannot create reply in Community'`, and mutation-killed the fix by deleting the `else:`/`log_incoming_ap` pair (reproduced `sqlalchemy.exc.NoResultFound`) |
| D134 | `process_new_content`, both halves (`app/activitypub/routes.py:2352-2356` post, `:2399-2405` reply) | **FIXED, commit `1ffd75b5`.** The post half's refused-creation branch (`create_post` returned `None`, `:2352-2354`) neither logged the refusal nor returned -- control fell off the end of the enclosing `if not in_reply_to:` block and off the end of `process_new_content` itself. Fix: added `log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Post creation refused')` and an explicit `return` (`:2355-2356`). The same commit gave the reply half's mirror branch (already `return`-ing, at `:2406`, but not logging) the equivalent `log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Reply creation refused')` at `:2405`, so the two halves end consistent -- both log the refusal, both return. | fixed and verified | measured: Task 8/9 inverted `test_a_refused_post_is_deleted_remotely_and_logs_nothing` to `..._and_logged` (`tests/test_inbox_dispatch_new_content.py:461`) and `test_a_refused_reply_is_deleted_remotely_and_logs_nothing` to `..._and_logged` (`:790`), both asserting exactly one `ActivityPubLog` row via `.one()`. Three separate mutation kills: deleting the post half's `log_incoming_ap` call kills the post test (`NoResultFound`); deleting the reply half's kills the reply test (`NoResultFound`); deleting the post half's `return` alone kills neither test ("28 passed") and is recorded as structurally justified but observationally unkillable -- the function has no code after this branch, so falling off the end is behaviourally identical to returning |
| D135 | `process_new_content` (`app/activitypub/routes.py:2312-2316`) | **FIXED, commit `e092ff40`.** `activity_json['id'] = shorten_string(activity_json['id'], 100)` (pre-fix `:2313`) wrote the truncated id back into `activity_json`, which on the DIRECT path (`announced` False) IS `request_json` (`:2305`) -- a dict the dispatcher's caller still owns and uses afterwards. Fix: `activity_json = {**activity_json, 'id': shorten_string(activity_json['id'], 100)}` (`:2316`), rebinding the name to a shallow copy with only the top-level `id` replaced, so `request_json['id']` is left untouched. The copy is deliberately shallow -- `activity_json['object']` remains the same nested object the caller holds -- so `update_post_from_activity` / `update_post_reply_from_activity` / `create_post` / `create_post_reply` see identical nested content, and the truncated id still reaches `Post.ap_create_id` / `PostReply.ap_create_id` (both `db.String(100)`, `app/models.py:1722` and `:2893`) via the same `activity_json` passed to `create_post` (`:2340`) / `create_post_reply` (`:2387-2388`). | fixed and verified | measured: Task 8's `test_the_id_truncation_mutates_the_callers_activity` pinned the pre-fix mutation; Task 9 inverted it to `test_the_id_truncation_leaves_the_callers_activity_untouched` (`tests/test_inbox_dispatch_new_content.py:852`), asserting the caller's `activity['object']['id']` is unchanged while the value `create_post` actually receives is still truncated to 98 characters (`args[2]['id'] == long_id[:97] + '…'`). Two mutations killed: restoring the in-place assignment reproduces the original failure; copying the dict but skipping the truncation (`activity_json = dict(activity_json)`) fails the 98-character assertion, proving the suite forbids the tempting wrong fix that would overflow the `String(100)` column. Fix round 1 additionally pinned this fix's federation-behaviour consequence on the DIRECT path (recorded separately below as a deliberate behavioural change) |

### 2. Four items registered, not fixed -- D136-D139

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D136 | `process_new_content` (`app/activitypub/routes.py:2318-2320`) | **Corrected, not a defect -- the premise carried forward by the spec and two task briefs does not hold once the function is read.** The design spec (`docs/superpowers/specs/2026-09-02-coverage-new-content-7-design.md:180`), Task 9's brief (`.superpowers/sdd/2026-09-02-coverage-new-content-7/task-9-brief.md:67`) and Task 10's own brief (`task-10-brief.md:16`) all state that `find_microblogging_community()` "can return `None`", after which `community` is dereferenced unguarded in both halves. Reading `find_microblogging_community()` itself (`app/activitypub/util.py:4607-4626`) shows no branch in its body returns `None`: it queries for an existing `Community` row (`instance_id == 1, user_id == 1, name == 'microblogs'`), and if none exists, constructs one, commits it, and returns it unconditionally at `:4626`. A `return None` does sit a few lines above, at `util.py:4604`, but it belongs to the *preceding* function in the file, not this one. So once `community = find_microblogging_community()` (`routes.py:2320`) runs to completion without raising, `community` is guaranteed non-`None`; the only way it could still be `None` afterward is if the call raised (e.g. `db.session.commit()` failing under a race with another process creating the same row) rather than returned one -- a different failure mode from the unguarded-`None`-dereference this item has been carried as. | corrected, not a defect | reading-level: `app/activitypub/util.py:4607-4626` read in full; no `return None` exists in this function's body, and the `return None` at `:4604` is the last line of the preceding function, confirmed by locating both functions' `def` lines. No test in this suite exercises `find_microblogging_community` un-doubled -- `tests/test_inbox_dispatch_new_content.py:188` doubles it to return a real `microblog` object -- so the tests establish nothing about the commit-race exception path either, but that is a narrower and different claim from the one being corrected here |
| D137 | `process_new_content`, both halves (`app/activitypub/routes.py:2328`, `:2372`) versus the `Move` handler (`app/activitypub/routes.py:1606`) | **Not fixed -- ruled KEEP-AS-IS.** `app/activitypub/routes.py:1606` guards a community move with `user.id == post.user_id or origin_community.is_moderator(user) or (origin_community.instance_id == user.instance_id and origin_community.is_instance_admin(user))` -- a stricter instance-admin idiom than either half of `process_new_content` (`:2328`, `:2372`), which grant the right on `is_instance_admin(user)` alone, with no same-instance conjunct. Ruled KEEP-AS-IS: `Community.is_instance_admin` (`app/models.py:752-757`) already scopes its `InstanceRole` lookup to the community's own `instance_id`, so the looser form used by both halves of `process_new_content` cannot grant edit rights to an admin of an unrelated instance -- the extra conjunct at `:1606` is defence-in-depth that would apply equally to both halves, and adopting it belongs in its own change, not folded into D132's symmetry fix (which deliberately copied the post half's existing guard verbatim so the two halves would agree). | not fixed, keep-as-is | reading-level: `app/activitypub/routes.py:1606,2328,2372` and `app/models.py:752-757` all read directly; no test in this suite pins the guard either way, since no test seeds an `InstanceRole` for an admin of an instance other than the community's own |
| D138 | `announce_activity_to_followers` (`app/activitypub/routes.py:1958-1959`) | **Not fixed, registered only -- out of scope, same defect class as D135.** `if '@context' in activity: del activity["@context"]` mutates the caller's dict in place -- the identical class of defect D135's fix repaired for `activity_json['id']`, but in a different function that this sub-project's brief does not cover: `announce_activity_to_followers`, not `process_new_content`. | not fixed, out of scope | reading-level: `app/activitypub/routes.py:1958-1959` read directly; no test in this sub-project's suite asserts on the caller's dict after an `announce_activity_to_followers` call, so nothing here was observed, only read |
| D139 | delegate functions in `app/activitypub/util.py` (`:2614`, `:2992`, `:3127`) | **Not fixed, registered only -- a known limit of D135's fix, not a regression it introduced.** All three lines do `request_json['object']['content'] = '<p>' + request_json['object']['content'] + '</p>'`, writing through the nested `['object']` dict that D135's shallow copy deliberately leaves shared with the caller (the copy replaces only the top-level `'id'` key; `activity_json['object']` remains the very object the caller holds). These three writes reached the caller's structure identically before D135's fix and reach it identically after -- pre-existing, unchanged, and outside the defect D135 addressed. | not fixed, known limit of D135 | reading-level: `app/activitypub/util.py:2614,2992,3127` read directly and confirmed identical in shape at all three sites; Task 9's own report made and verified the same claim while correcting an earlier, broader one ("the caller's structure is never written to") that these three lines falsify as an absolute |

### 3. One deliberate behavioural change, ruled KEEP -- D140

| # | function | change | status | evidence |
|---|---|---|---|---|
| D140 | `process_new_content`'s DIRECT path, via D135's fix (`app/activitypub/routes.py:2316`), consumed by `announce_activity_to_followers(..., request_json)` at `app/activitypub/routes.py:2332`, `:2350`, `:2377` and `:2398` | **Not a defect -- deliberate behavioural change, ruled KEEP.** Before D135's fix, the DIRECT path's four `announce_activity_to_followers` calls (all under `if not announced:`) relayed `request_json` after its `'id'` had already been truncated in place; after the fix, they relay the caller's **original, untruncated** activity id. Ruled KEEP: the truncated form is `s[:97] + '…'` (`shorten_string`, `app/utils.py:1600`) containing U+2026, which no receiving instance can dereference as a URI, and two origin activities sharing a 97-character prefix would collapse onto the same relayed id. The truncation exists only to fit this instance's own `String(100)` `ap_create_id` column (`app/models.py:1722`, `:2893`) and is preserved for that purpose -- D135's fix narrows only what gets written back into the caller's dict, not what gets stored. Preserving the old relay behaviour would have required *adding* a second, deliberate truncation, a strictly larger change than the one made. | deliberate change, ruled keep | measured: `test_a_create_that_succeeds_logs_success_and_announces` (`tests/test_inbox_dispatch_new_content.py:361`) asserts `args[2]['id'] == long_activity_id` (the untruncated value) off `announce_activity_to_followers`'s captured call. This assertion did not exist when D135 first landed -- Task 9's own review found the original pin used the ANNOUNCED shape, where all four call sites are unreachable under `if not announced:`, so this federation-behaviour consequence had been asserted by no test at all; it was added in the same fix round as D132's missing-branch repair, and mutation-killed by re-truncating immediately before the direct-path announce call |

### 4. One residual asymmetry, registered but not fixed -- D141

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D141 | `process_new_content`, both halves' existing-content edit paths (`app/activitypub/routes.py:2328-2329` post, `:2372-2373` reply) | **Not fixed, registered only -- reading-level.** Once the outer author/moderator/instance-admin guard passes (post: `:2328`, reply: `:2372`), the reply half gates the actual edit behind a second, inner check, `can_create_post_reply(user, community)` (`:2373`); the post half's mirror calls `update_post_from_activity(post, activity_json)` straight away (`:2329`) with no equivalent inner check at all. So a moderator or instance admin who fails `can_create_post(user, community)` can still edit an existing POST in a community they are not permitted to post in, while the identical actor failing `can_create_post_reply` is refused editing an existing REPLY there. D132 (#1 above) made the two halves' OUTER guards symmetric; this INNER difference is a separate gap the same fix round did not touch, and is arguably more consequential than D132's, since it is reachable by exactly the moderators and instance admins the outer guard exists to trust. No fix is proposed here -- this is a registration, not a recommendation, since closing the gap either way (adding the check to the post half, or removing it from the reply half) is a policy decision outside this sub-project's bounded authorisation. | not fixed, registered only | reading-level: `app/activitypub/routes.py:2328-2329` (post) and `:2372-2373` (reply) read side by side; no test in this suite doubles `can_create_post` to False on the post-half edit path to demonstrate the asymmetry end to end, though `test_a_permitted_editor_who_cannot_reply_is_logged`'s docstring (`tests/test_inbox_dispatch_new_content.py:614-615`) already states the same finding: "The post half has no equivalent inner check at all, so there is nothing to mirror this on that side" |

Six branch arcs remain partial in `process_new_content` beyond D118's debug
`pass` (`[2297,2298]`): `[2349,2351]`, `[2362,2364]`, `[2376,2380]`,
`[2397,2406]`, `[2403,2405]`, `[2413,2416]` (confirmed against a full-suite
`coverage.json`). None is a regression -- every one of the six was already
partial before this sub-project's fixes. The cause is a test asymmetry, not a
code one: the reply half has no announced-shape test at all (every
`announced_activity(...)` call in `tests/test_inbox_dispatch_new_content.py`
exercises the post half), and of the function's four `community.is_local()`
call sites (`:2353`, `:2362`, `:2403`, `:2413`), only `:2353` has its False
side covered (`test_a_refused_post_in_a_remote_community_is_not_deleted`).
D134's "the two halves end consistent" is true of the CODE; it is the TESTS,
not the code, that remain unmirrored between the two halves.

## Sub-project 8: `webfinger`/`process_webfinger_request`, the ActivityPub discovery endpoint

`docs/superpowers/specs/2026-09-02-coverage-webfinger-8-design.md` and
`docs/superpowers/plans/2026-09-02-coverage-webfinger-8.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-02-coverage-webfinger-8/`, not committed), on
branch `blentz`. Eight tasks brought `/.well-known/webfinger`'s two
functions -- `webfinger` (`app/activitypub/routes.py:56-71`) and
`process_webfinger_request` (`:74-170`) -- to full statement coverage but
for one statement in `webfinger` (`:60`, an equivalent mutant, see D147
below), and fixed three defects across three commits, all under this
sub-project's own bounded, explicit authorisation; this task (9) closes the
sub-project out with the findings register, the test-harness log, and the
coverage floor. `app/activitypub/routes.py` measures **74.7804% blended**
(1385/1832 statements, 658/900 branches) after this sub-project, up from
71.7423% before it (full suite: 3155 passed, 3 skipped, 6 subtests passed,
178.53s; 3106 passed before this sub-project). Tests live in
`tests/test_webfinger.py` (49 test functions, none parametrized, added
across Tasks 1-8). `process_webfinger_request` (`:75-170`) has **zero**
uncovered statements.

Like 5b-5e, 6 and 7 before it, this sub-project carried a **narrow,
explicitly authorised exception** to the campaign's report-don't-fix rule:
Task 8 was separately authorised to fix the three defects Task 7's pins and
the whole-branch review had established, all confined to `webfinger`/
`process_webfinger_request`. `git diff --stat app/` is non-empty for
exactly Task 8's three fix commits (`59c0a203`, `866e27bb`, `a350c99d`);
every other task's `git diff --stat app/` is empty.

### 1. Three defects fixed, test-first with a mutation-proved test -- D142-D144

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D142 | `process_webfinger_request`, both Feed lookups (`app/activitypub/routes.py:126-127` fallback, `:130-131` tilde) | **FIXED, commit `59c0a203`.** The three resolution lookups disagreed on visibility: `User` excluded deleted and banned accounts (`ap_id=None, deleted=False, banned=False`, `:117-120`), `Community` excluded `local_only` (`ap_id=None, local_only=False`, `:123`), but the `Feed` lookup filtered only `ap_id=None` -- no guard at all. `Feed.public` defaults to `False` (`app/models.py:4062`), so webfinger published private, banned and soft-deleted feeds, name and `public_url()` included, to any instance that asked, unauthenticated. Fixed by adding `public=True, banned=False, ap_deleted_at=None` to both textually-identical occurrences of the lookup (the non-tilde fallback and the `feed = True` branch), mapped one-to-one onto the existing `User`/`Community` guards: `banned=False` onto `User.banned` (`app/models.py:4081`, direct match), `ap_deleted_at=None` (`:4076`) onto `User.deleted=False` (`Feed` has no `deleted` boolean, so its soft-delete marker plays that part), `public=True` onto `Community.local_only=False` (both are the model's own federation-visibility flag). `is_instance_feed`, `searchable` and `nsfw` were deliberately not added -- role, search-only and content-label flags respectively, not visibility. This fix did not touch the Community lookup itself: it still guards only `ap_id=None, local_only=False` (`:123`), with neither a ban nor a soft-delete guard, so this row's one-to-one mapping onto the User/Community guards should not be read as meaning the three lookups are now consistent overall -- see D153. | fixed and verified | measured: six tests (three conjuncts x two occurrences) each sole-kill one dropped conjunct on one occurrence -- e.g. dropping `public=True` from the fallback copy is sole-killed by `test_a_private_feed_is_not_served` ("1 failed, 48 passed"), the same conjunct dropped from the tilde copy is sole-killed by `test_a_tilde_resource_for_a_private_feed_is_not_served`, and so on for `banned=False`/`ap_deleted_at=None`; all six are ASSERTION-kills, no crashes, proving the two textually-identical copies are independently pinned |
| D143 | `process_webfinger_request` (`app/activitypub/routes.py:134-135`) | **FIXED, commit `866e27bb`.** `if object is None:` returned the bare string `''` at HTTP 200 -- an unknown actor was indistinguishable, by status, from a malformed request or (before D142) a successful-but-suppressed lookup. Fixed with `abort(404)` in place of `return ''`. `abort()` was chosen over a hand-built `('', 404)`/`jsonify(...), 404` for three reasons: it is the idiom the same function already uses for the malformed-resource case two lines up and `webfinger()` uses for its own missing-`resource` case (`:71`); the app has a `/.well-known/`-aware 404 handler already (`app/errors/handlers.py:8-18`); and `abort` **raises**, which keeps the miss out of the `@cache.memoize(timeout=60)` result the function is wrapped in (`:74`) -- a returned 404 would have been memoised for a minute, leaving a newly-created local actor invisible to a peer that asked one moment early (see D151 below for the cost of this). | fixed and verified | measured: `test_an_unknown_actor_is_404` asserts `response.status_code == 404`; before the fix it failed `assert 200 == 404`. Restoring `return ''` re-fails 18 of the 49 tests at once (`18 failed, 31 passed`) -- not a sole kill, because `abort(404)` is the shared exit of every miss path in the function (every "not served" test, both Feed-guard tests from D142, and the tilde not-found test all converge here), which is the correct and expected blast radius for the function's single shared exit point |
| D144 | `process_webfinger_request` (`app/activitypub/routes.py:86-87`) | **FIXED, commit `a350c99d`.** The `else` arm of the parse chain (neither `'acct:'` nor `'https:'`/`'http:'` in the query) returned the bare string `'Webfinger regex failed to match'` at HTTP 200 instead of a client-error status. Fixed with `abort(400, description='Webfinger regex failed to match')` in place of the bare return, the same `abort` idiom as D143 and for the same memoisation reason. The description string does not reach the wire: this app installs flask_smorest (`rest_api = Api()`, `app/__init__.py:97`), whose app-wide `HTTPException` handler renders `{"code": 400, "status": "Bad Request"}` from its own `e.data` and ignores werkzeug's `description` -- see D150 below for why the string was kept anyway. | fixed and verified | measured: `test_a_malformed_resource_is_400` asserts `response.status_code == 400`; before the fix it failed `assert 200 == 400`. Restoring the bare-string return re-fails it as a sole death (`1 failed, 48 passed`) |

### 2. Nine items registered, not fixed -- D145-D153

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D145 | `process_webfinger_request` (`app/activitypub/routes.py:78,80`) | **Not fixed -- policy decision outside this slice's authorisation.** `query = resource` then `actor = query.split(':')[1].split('@')[0]` discards everything after `@`: `acct:alice@evil.example` is resolved and answered as this instance's own `alice`, with the reply's `subject` field asserting `alice@test.piefed.local` -- a handle the query never asked about. RFC 7033 expects a server to answer only for resources it is authoritative for. Rejecting a foreign domain would change who this instance answers for and could break federation with peer software that queries loosely (e.g. omits or gets the domain wrong); which behaviour is correct is a policy call, not a coverage-slice fix. | not fixed, policy decision | measured: `test_a_query_for_another_domain_is_answered_with_our_own_user` (`tests/test_webfinger.py:820`) queries `acct:alice@evil.example` and asserts `response.status_code == 200` and `response.json['subject'] == 'acct:alice@test.piefed.local'`, demonstrating the discarded domain end to end; the test's docstring documents this as accepted behaviour, not a pinned bug |
| D146 | `process_webfinger_request` (`app/activitypub/routes.py:79,84`) | **Not fixed, registered only.** `if 'acct:' in query:` (`:79`) is a substring test, not a prefix test, and it runs before the URL branch (`elif 'https:' in query or 'http:' in query:`, `:84`). So a resource like `https://evil.example/acct:bob` -- where the literal substring `'acct:'` appears anywhere in the string, not as its start -- takes the acct branch and is parsed as `actor = 'https' ... ` via `query.split(':')[1].split('@')[0]` rather than the URL branch's last-path-segment logic. | not fixed, registered only | reading-level: established by reading the `if`/`elif` order at `:79` and `:84` directly. No test in `tests/test_webfinger.py` supplies a resource where `'acct:'` occurs anywhere but as the literal prefix -- `test_a_tilde_prefix_is_not_recognized_in_url_form` (`:697`) is the closest existing test to this branch pair and it uses a URL with no `'acct:'` substring at all -- so the tests establish the branch ORDER (acct checked first) but nothing about what a query containing `'acct:'` as a non-prefix substring actually resolves to |
| D147 | `webfinger` (`app/activitypub/routes.py:58-60`) | **Not fixed -- equivalent mutant / dead branch, same class as D95/D96/D103.** `if not hasattr(g, 'site'): g.site = db.session.query(Site).get(1)` (`:59-60`) is reached only when `requestor_domain()` returns truthy (`:58`). `app/request_hooks.py`'s `before_request` (`:94-96`) already runs `g.site = Site(**site)` unconditionally for every request whose path is neither `/inbox` nor under `/static/` -- which includes `/.well-known/webfinger` -- before any view function runs. So by the time `webfinger()`'s body executes, `hasattr(g, 'site')` is always `True` and `:60`'s assignment is dead on every reachable input; the guard cannot fire through the route by construction. This is why full statement coverage of `webfinger` is unachievable through the route -- `:60` is its sole uncovered line. | not fixed, informational -- dead code, not reachable through the route | reading-level for the deadness mechanism (`app/request_hooks.py:94-96` and `app/activitypub/routes.py:58-60` read directly, both confirming `before_request` runs first and unconditionally for this path); corroborated by measured coverage: `:60` is `webfinger`'s only uncovered line in the full-suite run (3155 passed, 3 skipped, 6 subtests passed) |
| D148 | `process_webfinger_request`, all three lookups (`app/activitypub/routes.py:118,122` vs `:126,130`) | **Not fixed, registered only -- a third asymmetry among the three lookups.** The `User` lookup compares `func.lower(User.user_name) == actor.strip().lower()` (`:118`) and the `Community` lookup builds its profile id from `actor.strip().lower()` (`:122`) -- both case-insensitive -- while both `Feed` lookups compare `name=actor.strip()` (`:126`, `:130`) with no `.lower()` -- case-sensitive. | not fixed, registered only | measured for the User/Feed halves of the asymmetry: `test_a_user_is_matched_case_insensitively` (`:291`) queries `acct:ALICE@...` against a user stored as `alice` and gets 200; `test_a_feed_name_lookup_is_case_sensitive` (`:569`) queries `acct:NEWS@...` against a feed stored as `news` and gets 404. reading-level for the Community half: `:122`'s `.lower()` is confirmed by reading the source, but no test in this suite queries a community with mismatched case to demonstrate it independently -- a gap Task 5's ledger entry recorded and this task leaves unclosed |
| D149 | `process_webfinger_request` (`app/activitypub/routes.py:113,116,124,128,132`) | **Not fixed, registered only.** `object` (`:113`, and reassigned at `:117,123,126,130`) and `type` (`:116`, reassigned at `:124,128,132`) shadow Python builtins throughout the function's body. Cosmetic -- neither name is used as the builtin anywhere in this function -- but a maintenance hazard for a future edit that needs `isinstance(x, object)` or `type(x)` inside this scope. | not fixed, registered only | reading-level: both names' every assignment site read directly in the current file |
| D150 | `process_webfinger_request` (`app/activitypub/routes.py:87`) | **Not fixed -- correcting the retained string would be a fourth production edit outside this sub-project's three authorised fixes.** D144's fix kept `description='Webfinger regex failed to match'` on the `abort(400, ...)` call. The sentence says "regex", but there is no regex anywhere in the parse chain -- only the two `in` membership tests at `:79` and `:84`. The description is unobservable through HTTP in this app (see D144), so nothing depends on its wording, but it remains available to logging/Sentry/werkzeug's own default renderer with an inaccurate diagnosis. | not fixed, out of scope | reading-level: `:79,84,87` read directly; no `re` import or regex pattern exists anywhere in `process_webfinger_request` |
| D151 | `process_webfinger_request` (`app/activitypub/routes.py:74`), consumed via `/.well-known/webfinger`'s absence of a rate limit (`app/__init__.py:204`) | **Not a defect in D143's fix -- a trade-off worth recording.** `process_webfinger_request` is `@cache.memoize(timeout=60)` (`:74`), and `CACHE_TYPE` defaults to `FileSystemCache` in production (`config.py:38`), so the memo is live there (test config overrides it to `NullCache`, see the harness fact below). Before D143, a returned `''` for a miss would have been memoised for 60 seconds -- a real negative cache. `abort` raises rather than returns, so D143's fix correctly stops a newly-created local actor staying invisible to a peer that queries one moment early, but it also means a genuine miss is now never cached: `/.well-known/webfinger` carries no rate limit (`app/__init__.py:204` is a bare `limiter.init_app(app)`, and the `Limiter(...)` construction at `:92` passes no `default_limits`), so every unknown-actor query now costs up to three DB round-trips (`User`, `Community`, `Feed`) where it previously cost that once per minute per resource. | not a defect, trade-off recorded | reading-level by necessity: `.env.test` sets `CACHE_TYPE=NullCache` (`.env.test:11`), so no test in this suite's config can demonstrate a live memo either way; `config.py:38`, `app/activitypub/routes.py:74`, and `app/__init__.py:92,204` read directly for the production-path claim |
| D152 | `webfinger`/`process_webfinger_request`'s two error paths (`app/activitypub/routes.py:71,135` vs `:87`) | **Not a defect in either fix -- a consequence of the two fixes landing on different status codes, worth recording.** `app/errors/handlers.py` registers `@bp.app_errorhandler(404)` (`:8`) with a `/.well-known/`-path special case that renders `errors/404.html` (`:11-18`), but registers no handler for 400. `rest_api = Api()` (`app/__init__.py:97`, flask_smorest) registers a handler for the `HTTPException` *class*, not any specific code (`flask_smorest/error_handler.py:31`, `register_error_handler(HTTPException, ...)`). Flask's own handler lookup (`flask/sansio/app.py:823-845`, this environment's installed Flask) tries a code-specific app handler before an exception-class app handler, in that order. So a 404 (D143) resolves to PieFed's own code-specific handler and renders as `text/html`; a 400 (D144) has no code-specific handler anywhere and falls through to flask_smorest's class handler, rendering as JSON (`{"code": 400, "status": "Bad Request"}`, confirmed in D144's evidence). The endpoint now answers a miss in HTML and a malformed request in JSON. | not a defect, consequence recorded | reading-level: `app/errors/handlers.py:8-18` (only 404/500/401/429 registered, verified by reading the whole file), `app/__init__.py:97`, `flask_smorest/error_handler.py:31`, and the installed `flask/sansio/app.py:823-845`'s `_find_error_handler` docstring and body all read directly. No current test asserts `content_type` on a 404 response -- Task 8's inversion dropped that assertion from all 18 converted tests -- so the HTML side is not measured, only read |
| D153 | `process_webfinger_request`, Community lookup (`app/activitypub/routes.py:123`) | **Not fixed here -- closing it would be a fourth production edit outside this sub-project's three authorised fixes.** D142's fix made the Feed lookup the strictest of the three (`ap_id=None, public=True, banned=False, ap_deleted_at=None`, `:126-127` and `:130-131`); the User lookup guards `ap_id=None, deleted=False, banned=False` (`:117-120`); the Community lookup guards only `ap_id=None, local_only=False` (`:123`) -- neither a ban nor a soft-delete guard. `Community.banned` (`app/models.py:591`) and `Community.ap_deleted_at` (`app/models.py:584`) both exist. So a banned or soft-deleted local community is still resolved and advertised by webfinger -- name and `public_url()` included -- to any unauthenticated caller, the same defect class D142 fixed for Feed, on a different actor type. Two pieces of evidence this is an oversight rather than a policy choice: the four sibling local-community actor endpoints in this same file all filter `banned=False` on the identical `Community.query.filter_by(name=actor, banned=False, ap_id=None)` shape (`app/activitypub/routes.py:2018`, `:2049`, `:2074`, `:2100`); and `app/admin/routes.py:1472` sets `community.banned = True` on the community deletion path itself, with the inline comment "hide this community from the UI by banning it" -- so a deleted community is expected, elsewhere in this same codebase, to read as banned, and webfinger's Community lookup is the one place that expectation is not enforced. | not fixed, registered only | reading-level: `app/activitypub/routes.py:117-120,123,126-127,130-131` read directly for the three lookups' current guards; `app/models.py:584,591` read directly for `Community.ap_deleted_at`/`Community.banned`'s existence; `app/activitypub/routes.py:2018,2049,2074,2100` read directly for the four sibling endpoints' identical `banned=False` guard; `app/admin/routes.py:1472` read directly for the deletion-path comment. No test in this suite queries a banned or soft-deleted local community through webfinger to demonstrate the gap end to end |

**Next free number: D154.** Taken by sub-project 9 (D154-D164) --
see the "Sub-project 9" section below for the table.

## Sub-project 9: the actor-profile endpoints -- `user_profile`, `community_profile`, `feed_profile`

`docs/superpowers/specs/2026-09-02-coverage-actor-profiles-9-design.md` and
`docs/superpowers/plans/2026-09-02-coverage-actor-profiles-9.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-02-coverage-actor-profiles-9/`, not committed), on
branch `blentz`. Eleven tasks brought the three actor-profile endpoints in
`app/activitypub/routes.py` -- `user_profile` (`:364-463`), `community_profile`
(`:510-614`) and `feed_profile` (`:2642-2730`) -- to full statement coverage
except for five statements, all in `community_profile`'s two authenticated
not-found redirect arms (see D162 below), and fixed two defects across two
commits (plus a follow-up commit closing a review finding against the second),
all under this sub-project's own bounded, explicit authorisation; this task
(11) closes the sub-project out with the findings register, the test-harness
log, and the coverage floor. `app/activitypub/routes.py` measures **80.3676%
blended** (1470/1826 statements, 716/894 branches) after this sub-project, up
from 74.7804% before it (1385/1832 statements, 658/900 branches, sub-project
8's own ending figure). Full suite after this sub-project: **3226 passed, 3
skipped, 6 subtests passed**, 333.56s. Tests live in `tests/test_actor_profiles.py` (71 test
functions, none parametrized, added across Tasks 1-10). `user_profile` and
`feed_profile` both have **zero** uncovered statements.

Like 5c through 8 before it, this sub-project carried a **narrow, explicitly
authorised exception** to the campaign's report-don't-fix rule: Task 10 was
separately authorised to fix the two defects the spec identified in
`user_profile`, confined to that function. `git diff --stat app/` is
non-empty for exactly Task 10's two fix commits (`d661a12b`, `ff0a9156`);
every other task's `git diff --stat app/` is empty.

### 1. Two defects fixed, test-first with a mutation-proved test -- D154-D155

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D154 | `user_profile` (`app/activitypub/routes.py:370-384` before the fix) | **FIXED, commit `d661a12b`.** The function opened `if current_user.is_authenticated and current_user.is_admin(): <six lines> else: <the same six lines, byte for byte>`, verified identical line for line. The comment above it read "admins can view deleted accounts" -- behaviour neither branch implemented, since neither filtered `deleted` or `banned` at the time (see D155). A pure no-op: both arms ran the identical `'@' in actor` lookup, the identical bare-username lookup and the identical `ap_profile_id` fallback. Collapsed to one copy; the false comment was removed with the branch it described rather than kept or "fixed" into a new feature, which was out of scope. `current_user` was referenced exactly once in `user_profile`, in the removed condition, so the collapse removes the function's only use of it; the import (`app/activitypub/routes.py:4`) stays live at four other call sites in the same file (`:484,606,610,2208`). | fixed and verified | measured: no mutation is possible on a no-op refactor, so proof is structural instead -- the two removed bodies were compared byte-for-byte and found identical (including indentation, with the survivor dedented by exactly four columns, nothing reordered, no changed binding, no early return); all 69 tests in the file passed unchanged before and after; and a targeted coverage diff showed the six removed statements were already uncovered before the change (the admin branch was never reached -- see the harness fact below), so nothing that was covered became uncovered. Reviewed and confirmed independently by a second agent, which re-extracted both bodies from source and re-ran the byte comparison itself |
| D155 | `user_profile`'s two local lookups (`app/activitypub/routes.py:373-381`) | **FIXED, commit `ff0a9156`, with a follow-up test-strengthening commit `f632a9dd`.** Neither local lookup filtered `deleted` or `banned`, so `/u/<actor>` served a deleted or banned local user's full ActivityPub document -- public key, inbox, shared inbox -- to any unauthenticated caller, while webfinger's User lookup in the same module correctly excludes both (`:117-120`). Fixed by adding `deleted=False, banned=False` to both local queries (the bare-username lookup and the `ap_profile_id` fallback beneath it), matching webfinger's shape. **This deliberately changes who is visible**: a deleted or banned local user's profile now 404s where it previously returned data, on the HTML path as well as the ActivityPub path, since the guard is on the lookup rather than the rendering. The remote (`ap_id`) lookup deliberately received no guard -- see D157. | fixed and verified | measured: `test_a_deleted_user_profile_is_not_served` and `test_a_banned_user_profile_is_not_served` each assert `404`, having first asserted the row still exists; against the unfixed code both failed `assert 200 == 404` (assertion, not crash -- the route ran to completion and returned a real document). Dropping `deleted=False` from both local queries at once sole-killed only the deleted test (1 failed, 68 passed); dropping `banned=False` sole-killed only the banned test. A review finding (M3) then showed the `ap_profile_id` fallback's own guards were unkillable in isolation -- `tests/factories.py`'s local-user factory always leaves `ap_profile_id` null, so the fallback query never matched in either guard test, and the original two mutations had dropped the clause from *both* local queries at once, proving the column rather than the query. `f632a9dd` added two more tests (`test_a_deleted_user_is_not_served_through_the_ap_profile_id_fallback`, `test_a_banned_user_is_not_served_through_the_ap_profile_id_fallback`) that give a user a mismatched `user_name` so only the fallback query can find the row, then re-ran all four single-clause mutations one call-site at a time -- two queries x two columns, every cell a sole assertion-kill, no crashes |

### 2. Nine items registered, not fixed -- D156-D164

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D156 | `user_profile`'s remote lookup (`app/activitypub/routes.py:371`) | **Not fixed -- a federation-behaviour question beyond this slice's authorisation.** `user_profile` serves a **remote** actor's ActivityPub document for an AP-Accept request, while `community_profile` (`:517-518`) and `feed_profile` (`:2653-2656`) both `abort(400)` for the equivalent case, each with the inline comment "don't provide activitypub info for remote communities". So this instance answers a query about someone else's actor with a document carrying **our** `sharedInbox` (`:412`) and `attributionDomains` (`:415`). Whether that is impersonation or a deliberate convenience for a client-side proxy is a policy call this slice should not settle alone. | not fixed, policy decision | reading-level: `app/activitypub/routes.py:371` (the unguarded remote branch), `:515-518` and `:2651-2656` (the two `abort(400)` guards and their comments), `:412,415` (the two locally-scoped fields in the served document) all read directly |
| D157 | `user_profile`'s remote lookup (`app/activitypub/routes.py:371`) | **Not fixed, deliberately, even after D155's fix to the local lookups.** `User.query.filter_by(ap_id=actor.lower()).first()` carries no `deleted`/`banned` guard. Three reasons stated in the fix's own report: it is outside the scope D155 was authorised for (the local lookups only); no test in `tests/test_actor_profiles.py`, `tests/test_remote_handle_resolution.py` or `tests/test_request_hooks.py` would discriminate one (all four remote-handle tests query a handle for which no row exists at all); and `banned` on a *remote* user means "banned by this instance", so `/u/name@host` with an HTML Accept is a moderator-facing view of that account -- 404-ing it would remove a path nothing in this sub-project examined. | not fixed, deliberate | reading-level: `app/activitypub/routes.py:371` read directly; measured that no existing test's coverage would change if the guard were added, since all remote-branch requests in the suite target a nonexistent row |
| D158 | All three functions' local lookups (`app/activitypub/routes.py:373-381` user, `:522` community, `:2659` feed) vs. their remote lookups (`:371` user, `:519` community, `:2657` feed) | **Not fixed -- the three endpoints disagree with each other on ban guards, and now disagree in opposite directions.** `community_profile` and `feed_profile` both filter `banned=False` on their **remote** lookups (`:519`, `:2657`) but not on their **local** ones (`:522`, `:2659`); after D155, `user_profile` has the exact opposite asymmetry -- guarded local lookups, unguarded remote lookup. Cross-reference **D153** (sub-project 8), which registered the same class of gap for webfinger's Community lookup; this is the same systematic-oversight pattern recurring across a third and fourth endpoint pair. | not fixed, registered only | reading-level: all six lookups' filter clauses read directly at the cited lines |
| D159 | All three functions' `id`/`preferredUsername` construction (`app/activitypub/routes.py:397-398` user; `:530,534` community; `:2669,2672` feed) | **Not fixed -- `preferredUsername` reflects the caller's casing in all three; `id` does too in two of the three, but not `user_profile`'s.** `community_profile` and `feed_profile` both hand-build `id` from the raw `actor` path segment rather than calling the model's own `public_url()` (community: `"id": f"https://{server}/c/{actor}"` at `:530`; feed: `"id": f"https://{server}/f/{actor}"` at `:2669`), and both set `"preferredUsername": actor` directly (`:534`, `:2672`) -- while resolving the row with `actor.lower()` (`:521-522` community, `:2657/:2659` feed). So `/c/BOOKS` returns `id` `https://.../c/BOOKS` and `preferredUsername` `BOOKS` for a community whose canonical profile is lowercase, and the same for `/f/`. **`user_profile` differs**: its `id` is `user.public_url()` (`:397`), which returns the row's own `ap_public_url` column when set, and only falls back to `f"{SERVER_URL}/u/{self.user_name}"` (`app/models.py:1449-1450`) when it is not -- either way built from the resolved row's own stored data, not from the request path -- so `user_profile`'s `id` is stable regardless of caller casing. Only its `preferredUsername` (`"preferredUsername": actor`, `:398`) carries the caller's raw casing. A remote instance that stores what it is told holds a `preferredUsername` differing from the canonical one for all three functions, but a divergent `id` only for `community_profile` and `feed_profile`. | not fixed, registered only | reading-level: each function's `id`/`preferredUsername` lines, the `.lower()` in each lookup, and `User.public_url()` at `app/models.py:1449-1450` all read directly |
| D160 | `app/activitypub/routes.py:455` (user), `:596` (community), `:2723` (feed) | **Not fixed -- three different `Cache-Control` max-ages for three documents of the same kind, with no evident reason.** `user_profile` sets `max-age=15`, `community_profile` `max-age=30`, `feed_profile` `max-age=5`. | not fixed, registered only | reading-level: all three `resp.headers.set('Cache-Control', ...)` lines read directly |
| D161 | `feed_profile` (`app/activitypub/routes.py:2721-2726`) | **Not fixed -- `feed_profile` never sets `Vary: Accept`, while the other two do.** `user_profile` (`:456`) and `community_profile` (`:597`) both call `resp.headers.set('Vary', 'Accept')`; `feed_profile`'s equivalent block (`:2721-2726`) has no such call. Its body depends entirely on the `Accept` header (ActivityPub JSON vs. `show_feed`'s HTML), so a shared cache may serve the ActivityPub JSON to a browser or the HTML to a remote instance. Note the observable shape, established by sub-project 9's own Task 3 finding: Flask-Compress's `after_request` hook appends `Accept-Encoding` to every response's `Vary` unconditionally, so `community_profile` yields `'Accept, Accept-Encoding'`, not bare `'Accept'` -- and `feed_profile` yields `'Accept-Encoding'` alone, never an absent header. A test asserting `'Vary' not in response.headers` against `feed_profile` would therefore fail against real output; the defect is that `Accept` is missing *from* `Vary`, not that `Vary` is missing entirely. | not fixed, registered only | measured: `test_feed_profile_never_sets_vary` (or equivalent, `tests/test_actor_profiles.py`) asserts the discriminating shape directly (`response.headers.get('Vary') == 'Accept-Encoding'` and `'Accept,' not in response.headers.get('Vary', '')`), confirmed passing against the real unmutated route |
| D162 | `community_profile`'s not-found path (`app/activitypub/routes.py:603-614`) | **Not fixed -- branches on authentication and redirects to two different UI routes, while the other two `abort(404)` unconditionally.** `user_profile`'s not-found path is a bare `abort(404)` (`:463`); `feed_profile`'s is a bare `abort(404)` (`:2730`). `community_profile`'s has four arms: an AP request `abort(404)`s (`:604-605`), an authenticated request for a remote-looking actor (`"@" in actor`) flashes and redirects to `community.lookup` (`:606-609`), an authenticated request for anything else flashes and redirects to `community.add_local` (`:610-612`), and only an anonymous, non-AP request falls through to `abort(404)` (`:613-614`). **The two authenticated arms' bodies (`:607-609` and `:611-612`) are the ONLY uncovered statements left in the three functions**, and they are unreachable in this harness for the reason documented as harness fact 37 in `tests/README.md` (a test cannot authenticate after an earlier request in the same test) -- say that plainly rather than implying nobody got to them; a test that logged in *before* its first request would reach both arms. | not fixed, registered only; the residual uncovered statements are explained, not merely unaddressed | measured: full-suite coverage of `app/activitypub/routes.py` lists exactly `607, 608, 609, 611, 612` as uncovered inside `community_profile`, confirmed against the source at those five lines directly |
| D163 | `user_profile` route (`app/activitypub/routes.py:364`) vs. `community_profile` (`:510`) and `feed_profile` (`:2642-2643`) | **Not fixed -- only `user_profile` accepts `HEAD`.** `@bp.route('/u/<actor>', methods=['GET', 'HEAD'])` (`:364`) has an explicit `if request.method == 'HEAD':` branch (`:387-393`) that returns before the GET path's header-setting code. `community_profile` declares `methods=['GET']` only (`:510`); `feed_profile` declares two routes, the first with no explicit `methods=` (defaults to `GET`) and the second `methods=['GET']` (`:2642-2643`) -- neither has a HEAD branch or accepts the verb. | not fixed, registered only | reading-level: all three route decorators and `user_profile`'s HEAD branch read directly |
| D164 | `is_activitypub_request`, defined twice (`app/activitypub/util.py:2200` and `app/utils.py:1862`) | **Not fixed -- established `measured` in Task 1, confirmed independently here.** The two definitions are byte-for-byte identical (`diff` empty). `app/activitypub/routes.py` imports the `app/activitypub/util.py:2200` copy once (`:15`) and calls it five times across this slice's three functions -- `:388,394` (`user_profile`'s HEAD-branch check and its main AP-JSON check), `:524,604` (`community_profile`'s AP-JSON check and its not-found-path check), `:2661` (`feed_profile`'s AP-JSON check) -- plus five more call sites elsewhere in the file outside this slice (`:486,2149,2176,2219,2237`); `app/utils.py:1862`'s copy has no importers anywhere in the application -- verified by grepping every `import` statement in the codebase for the name, which returns exactly two importer lines, both importing the `app/activitypub/util.py` copy: `app/activitypub/routes.py:15` (this slice's own import) and `app/main/routes.py:17`, a second call site outside this slice entirely (`app/main/routes.py:1169`). Neither line imports from `app/utils.py`, which is the whole of the evidence that its copy has no importers. The `app/utils.py` copy is not wholly dead code: it has one caller in the same file, `login_required_if_private_instance` (`app/utils.py:1913,1917`), a decorator applied across dozens of routes elsewhere in the app -- but that call resolves to the same-module definition, not an import of it, and it plays no role in `user_profile`, `community_profile` or `feed_profile`. A change to the content-negotiation rule applied to the wrong copy would silently do nothing to these three endpoints. | not fixed, informational | measured: mutating the `app/activitypub/util.py:2200` copy in Task 1 killed one test per disjunct; the identical mutation applied to the `app/utils.py:1862` copy killed nothing in `tests/test_actor_profiles.py`. Confirmed again here by grepping every `import` statement in the tree for the name |

### 3. Two more defects found by this fix wave's whole-branch review -- D165-D166

Both registered here, **not fixed** -- out of the review's authorised scope,
which was tests, docstrings, comments and this register only.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D165 | `resolve_remote_handle` (`app/activitypub/routes.py:466-491`) | **Not fixed -- two of its own three guards are uncovered, despite this sub-project's report describing them as pinned.** `tests/test_remote_handle_resolution.py`'s four tests cover only the anonymous-caller guard (`:484-485`) and the exception-to-404 path (`:490-491`). The `'@' not in actor` guard (`:482-483`) is unreached: all four tests request `/u/wakko@mastodon.cloud`, a handle that always contains `@`. The AP-Accept guard (`:486-487`) is also unreached: `test_activitypub_request_does_not_resolve` sends the AP-Accept header but never authenticates, so it returns at the anonymous-caller guard (`:484-485`) before line `:486` is ever asked to branch true -- deleting the AP-Accept guard entirely would leave that test's `assert calls == []` green. Two cheap closures: the AP-Accept guard needs a test that logs in via `session_transaction()` **before** its first request (harness fact 37, `tests/README.md` -- a test cannot authenticate after an earlier request in the same test, because Flask-Login caches the loaded user on the app-context-scoped `g` for the life of the session-scoped `app` fixture) and then sends the AP-Accept header; the `'@' not in actor` guard needs one of Task 10's guard tests (`test_a_deleted_user_profile_is_not_served`, `test_a_banned_user_profile_is_not_served`) to stop stubbing `resolve_remote_handle` to `None` via `_double_the_renderers` and instead let it run for real against a bare (no `@`) actor name -- safe, because a bare name returns `None` at the first guard and never reaches `search_for_user`. | not fixed, registered only | reading-level plus measured: `scratch_full_cov.json`'s `app/activitypub/routes.py` entry lists `483` and `487` in `missing_lines` and `[482, 483]`/`[486, 487]` in `missing_branches`; `tests/test_remote_handle_resolution.py`'s four tests read directly, confirming all four target a handle containing `@` and that the AP-Accept test never authenticates |
| D166 | webfinger's User lookup (`app/activitypub/routes.py:117-120`) vs. `user_profile`'s local lookup (`:376`) | **Not fixed -- a fourth lookup asymmetry, on a column D158 did not name.** Webfinger matches `func.lower(User.user_name) == actor` **or** `func.lower(User.alt_user_name) == actor` (`:117-120`). `user_profile`'s bare-username local lookup matches only `func.lower(User.user_name) == actor.lower()` (`:376`) -- `alt_user_name` plays no part. So a user reachable by their alt name via webfinger 404s at `/u/<altname>`, the same class of cross-endpoint disagreement D158 registers for the `deleted`/`banned` guards, but on a different column entirely; `alt_user_name` appeared nowhere in this register before this entry. | not fixed, registered only | reading-level: `app/activitypub/routes.py:117-120` and `:376` read directly, side by side; no test in this sub-project or `tests/test_actor_profiles.py` drives a user with a distinct `alt_user_name` through both endpoints to observe the divergence, so this is derived from reading, not measured |

**Next free number: D297.** D165 and D166 were taken by this fix wave;
D167-D186 were taken by sub-project 10, D187-D199 by sub-project 11, D200-D212
by sub-project 12, D213-D235 by sub-project 13, D236-D259 by sub-project 14,
D260-D273 by sub-project 15, D274-D283 by sub-project 16 and D284-D296 by
sub-project 17
-- see the eight sections immediately below; D232-D235 were taken by
sub-project 13's final fix wave and live in subsection 6 of its section,
D252-D259 by sub-project 14's final fix wave and live in subsections 4 and 5 of
its section, D270-D273 by sub-project 15's final fix wave and live in
subsection 5 of its section, and
D232 and D219(c) were closed by sub-project 14's Task 11 and updated in place
there rather than renumbered, and D243 and D257 were closed by sub-project 15's
fixes and updated in place in sub-project 14's section the same way.
**Sub-project 16 renumbered, moved and edited nothing**; its subsection 5 is an
INDEX of the unguarded-peer-input family -- seventeen existing entries spanning
sub-projects 14, 15 and their fix waves, listed with their sites read at that
commit -- and it takes no number of its own. **Its final fix wave took D283**
and extended that index's rejection partition to D236-D283, again without
renumbering, moving or editing any existing entry.
**Sub-project 17 renumbered and moved nothing, and edited exactly one entry** --
D283's parenthetical split of its nine `author` hits, a residual sub-project 16
parked for it, corrected in place because it was wrong when written rather than
drifted (the total of nine and D283's verdict are unaffected). It also extended
that same index to **21 members + 36 rejections + 4 entries outside the
index's declared five-function scope = D236-D296**, describing the extension
in its own section rather than editing any row of the index. If you
take D297, say so here in the change that takes it.

## Sub-project 10: the nine ActivityPub collection endpoints

`docs/superpowers/specs/2026-09-02-coverage-collections-10-design.md` and
`docs/superpowers/plans/2026-09-02-coverage-collections-10.md` (design and plan;
the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-02-coverage-collections-10/`, not committed), on branch
`blentz`. Eleven tasks brought the nine collection endpoints in
`app/activitypub/routes.py` under test -- `community_outbox` (`:2013-2040`),
`community_featured` (`:2044-2065`), `community_moderators_route`
(`:2069-2091`), `community_followers` (`:2095-2111`), `user_followers`
(`:2115-2143`), `feed_outbox` (`:2739-2776`), `feed_following` (`:2780-2816`),
`feed_moderators_route` (`:2820-2847`) and `feed_followers` (`:2851-2871`) --
and fixed three defects across three commits (plus a follow-up commit correcting
two docstrings a sibling task had falsified), all under this sub-project's own
bounded, explicit authorisation; this task (11) closes the sub-project out with
the findings register, the test-harness log, and the coverage floor.
`app/activitypub/routes.py` measures **86.2221% blended** (1593/1831 statements,
760/898 branches) after this sub-project, up from 80.3676% (1470/1826
statements, 716/894 branches, sub-project 9's own ending figure). Full suite
after this sub-project: **3286 passed, 3 skipped, 6 subtests passed**. Tests live
in `tests/test_ap_collections.py` (53 test functions, none parametrized, added
across Tasks 1-9). The coverage figure above is the controller's single
authoritative module-level measurement taken after Task 10; **no per-function
residual breakdown was re-measured at Task 11**, so this section claims the
module figure and not "zero uncovered statements" for any individual function.

Like 5c through 9 before it, this sub-project carried a **narrow, explicitly
authorised exception** to the campaign's report-don't-fix rule: Task 10 was
separately authorised to fix the three unknown-feed crashes the pins written by
Tasks 6-9 had established, confined to the three feed functions that carry them.
`git diff --stat app/` is non-empty for exactly Task 10's three fix commits
(`ffc1eab4`, `26f703a5`, `db767352`); every other task's `git diff --stat app/`
is empty.

**The spec's own predictions were falsified in two places, and the falsifications
are the more useful findings.** The spec predicted the malformed feed join would
inflate `totalItems` observably; it does not, for a reason worth more than the
prediction was (D171). The spec predicted `community_moderators` would let a
community with no moderators publish an empty collection; it cannot, because the
function synthesises one (D174). Both are recorded as found, not as predicted.

### 1. Three defects fixed, test-first with a mutation-proved test -- D167-D169

All three are remotely-reachable HTTP 500s on `GET /f/<unknown>/...`, where every
community and user collection -- and `feed_followers`, the fourth feed collection
-- returns 404. **They are two different bugs, not one**, and this distinction
was itself a corrected review finding: a fix for D167 is not a fix for D168 or
D169, and the three needed three commits rather than one.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D167 | `feed_moderators_route` (`app/activitypub/routes.py:2827-2847`) | **FIXED, commit `ffc1eab4`.** The function resolved the feed and opened `if feed is not None:` with **no `else`**. A request for a feed that does not exist fell off the end of the view, the view returned `None`, and Flask raised `TypeError: The view function for 'activitypub.feed_moderators_route' did not return a valid response. The function either returned None or ended without a return statement.` So `GET /f/<anything>/moderators` was a 500 on every instance, triggerable by any remote peer requesting an arbitrary URL. Fixed by adding `else: abort(404)` (`:2846-2847`), matching `feed_followers`' **nested** shape twenty lines below (`:2870-2871`), which that sibling always had right -- nested here because the surrounding code was already nested. | fixed and verified | measured: `test_an_unknown_feed_moderators_is_404` witnessed failing before the fix with that exact `TypeError` (matched on Flask's specific "did not return a valid response" wording, which fires only on an implicit `None` return) and passing after. The mutation kill is a **crash-kill**, not an assertion-kill, and necessarily so -- see harness fact 44 in `tests/README.md`: the mutant raises before `assert response.status_code == 404` is ever reached, so an assertion-kill is structurally impossible for a crash-to-404 inversion, not merely unachieved. The same shape was independently demonstrated one task earlier, when deleting `feed_followers`' `else: abort(404)` reproduced this function's live bug exactly |
| D168 | `feed_outbox` (`app/activitypub/routes.py:2748-2757`) | **FIXED, commit `26f703a5`. A DIFFERENT MECHANISM from D167.** There was no `None` check at all: `feed: Feed = db.session.query(Feed).filter_by(name=actor.lower(), ap_id=None).first()` (`:2748`) was followed directly by `if not feed.public:` (`:2756`), so an unknown feed raised `AttributeError: 'NoneType' object has no attribute 'public'`. Fixed with a flat early guard, `if feed is None: abort(404)` (`:2751-2752`), deliberately **not** `feed_followers`' nested shape: this function already carries a flat `if not feed.public: abort(403)`, so nesting would have created a mixed idiom inside one function, and re-indenting the body would have dragged D171's and D172's deliberately-unfixed lines into the diff. The remote-actor branch is untouched -- `if '@' in actor: abort(400)` (`:2744-2746`) raises before `feed` is ever assigned, so the new guard sits in the local `else` arm only and cannot shadow the 400. | fixed and verified | measured: `test_an_unknown_feed_outbox_is_404` witnessed failing before the fix with that `AttributeError` and passing after; crash-kill, for the reason given under D167 |
| D169 | `feed_following` (`app/activitypub/routes.py:2786-2795`) | **FIXED, commit `db767352`.** Byte-identical to D168 in both defect and fix: no `None` check, `if not feed.public:` (`:2794`) dereferenced directly, `AttributeError` for an unknown feed; the same flat early guard added at `:2789-2790`. Registered as its own number rather than folded into D168 because it is a second function that must be fixed separately -- the campaign's own rule that a clause duplicated across two call sites has two ways to be wrong (harness fact 42) applies to a *missing* clause too. | fixed and verified | measured: `test_an_unknown_feed_following_is_404`, same shape, same crash-kill |

### 2. Seventeen items registered, not fixed -- D170-D186

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D170 | `feed_moderators_route`, the **found-feed** path (`app/activitypub/routes.py:2830,2840`) | **Not fixed -- a FOURTH crash in the same function, distinct from D167, and deliberately left open.** `Feed.user_id` (`app/models.py:4079`) is a plain FK column with **no declared default**, so a `Feed` row whose `user_id` was never set leaves `moderators = [db.session.query(User).get(feed.user_id)]` (`:2830`) as `.get(None)`, which returns `None`; the loop two lines later then reads `moderator.ap_profile_id` (`:2840`) and raises `AttributeError: 'NoneType' object has no attribute 'ap_profile_id'`. Where D167-D169 fire on an *unknown* feed, this one fires on a *known* one. **Three reasons it was not fixed with the other three.** (1) Unlike D167-D169, which any remote instance triggers by requesting an arbitrary URL, this needs a specific data state -- a feed with a null or dangling `user_id` -- whose reachability in production is **unproven**: the ordinary creation path sets the column (`app/shared/feed.py:183`, `Feed(user_id=user.id, ...)`), so establishing reachability means auditing every other feed-creation path, which is outside these nine functions. (2) There is **no pin** for it: `test_a_feed_moderators_collection_lists_its_owner` deliberately assigns an owner to avoid it, so a fix would have had no witnessed pre-fix failure, which this campaign requires of every fix. (3) The right answer is not settled by the spec -- 404, an empty collection, or skipping the absent moderator are all defensible, and each changes what peers are told, which is exactly the line the spec draws for registration. Cost if wrong: one more remotely-reachable 500 stays open one sub-project longer, already documented in the docstring that found it. | not fixed, ruled REGISTER by the controller | measured: established by *removing* the `feed.user_id = owner.id` assignment from `test_a_feed_moderators_collection_lists_its_owner`'s body and observing the `AttributeError` directly -- the brief's literal happy-path body, which omitted that assignment, crashed for this reason. `app/models.py:4079` and `app/shared/feed.py:183` read directly |
| D171 | `feed_outbox` (`app/activitypub/routes.py:2760`) and `feed_following` (`:2798`) | **Not fixed -- a genuinely malformed join that is currently invisible, and the reason it is invisible is the finding.** Both do `db.session.query(FeedItem).join(Feed, FeedItem.feed_id == feed.id)`. The `ON` condition never mentions the joined `Feed` table: `feed` is the already-resolved Python object, so `feed.id` is a bound literal and the clause compiles to `... JOIN feed ON feed_item.feed_id = %(feed_id_1)s`. Every matching `FeedItem` is therefore paired with **every row in `feed`** -- a real cartesian product, confirmed by raw SQL and `EXPLAIN`, not merely by reading. The intended query is a filter, not a join. **But it does not reach the response.** `feed_outbox` calls SQLAlchemy's *legacy* `Query.all()`, which auto-deduplicates mapper entities by identity: `Query._iter` (`sqlalchemy/orm/query.py:2859-2879` in the installed 2.0.52) checks `result._attributes.get("filtered")` and calls `result.unique()` **unconditionally** whenever the query returns mapper entities, and the per-entity unique filter (`sqlalchemy/orm/loading.py:184-196`) keys on Python's `id()` of the mapped object -- both cartesian rows carry the same `FeedItem.id`, so the Session's identity map hands back the *same* object and two rows collapse to one. (This is **not** the stricter `multi_row_eager_loaders` path at `loading.py:281-291`, which would force the caller to call `.unique()`; that path needs `joinedload`/`contains_eager`, and these are plain `.join()`s.) So the defect is **real in the SQL but latent**, masked by an ORM behaviour that is itself deprecated. A migration to 2.0-style `select()` -- which does *not* auto-deduplicate unless `.unique()` is called -- would expose it, inflating `totalItems` by the number of `Feed` rows on the instance. That is what makes it worth registering: it is a bug waiting on an unrelated upgrade, not a bug that duplicates today. Not fixed because correcting the join changes nothing observable now and the campaign's fix authorisation is spent on defects with witnessed failures. | not fixed, registered only | measured, and the masking is *pinned falsifiably*: `test_the_feed_outbox_malformed_join_is_masked_by_orm_deduplication` and `test_the_feed_following_malformed_join_is_masked_by_orm_deduplication` each seed one `FeedItem` and **two** `Feed` rows, assert `len(feeds) == 2`, and assert `totalItems == 1` **and** `totalItems != len(feeds)`. The second feed exists solely to make the non-match visible; a one-feed version of either test would be indistinguishable from the ordinary path. Four query forms were compared side by side against identical data: raw SQL 2 rows, `select()` without `.unique()` 2 rows, `select().unique()` 1 row, legacy `Query.all()` (what the route calls) 1 row -- exactly consistent with the mechanism above rather than coincidental. Under a `.unique()`-less `select()` rewrite both tests would fail, which is the point of them. The SQLAlchemy source citations were checked against the installed 2.0.52, not taken from the report that first offered them. **The spec predicted `totalItems == 2` here and was wrong** |
| D172 | `feed_outbox` (`app/activitypub/routes.py:2762-2765`) vs. `feed_following` (`:2800-2805`) | **Not fixed -- the endpoint whose own comment calls it equivalent to its twin publishes what the twin deliberately withholds, and renders it differently too.** `feed_outbox`'s header comment (`:2740-2742`) says it "will just be the same as the /following collection". Two ways it is not. (a) `feed_following` skips communities that are `local_only` or `private` (`:2803-2804`, `if c.local_only or c.private: continue`); `feed_outbox` has no such filter, so a `local_only` community's URL is published to every federating peer through `/f/<name>/outbox` while `/f/<name>/following` withholds it. (b) `feed_outbox` appends `c.ap_public_url` (`:2765`) where `feed_following` appends `c.public_url()` (`:2805`). These are not the same expression: `Community.public_url()` (`app/models.py:791-793`) returns `ap_public_url` when set and falls back to `f"{SERVER_URL}/c/{self.name}"` when it is not, so a community with a null `ap_public_url` contributes a literal `null` to `feed_outbox`'s `items` array and a real URL to `feed_following`'s. Not fixed: the leak is a federation-visibility change (it removes URLs peers are currently told about), and the two disjuncts of the filter mean three defensible fixes rather than one. | not fixed, registered only | measured for (a): `test_the_feed_outbox_publishes_local_only_communities` seeds a community with `local_only = True` set explicitly (it defaults to `False`) and asserts `community.ap_public_url in response.json['items']`; the withholding half is pinned per-disjunct by `test_feed_following_skips_local_only_communities` and `test_feed_following_skips_private_communities`, each of which sets the *other* flag to `False` by hand rather than leaning on a factory or column default, and each of which sole-killed its own disjunct as an assertion-kill. Reading-level for (b): `:2765`, `:2805` and `app/models.py:791-793` read directly |
| D173 | `community_outbox` (`app/activitypub/routes.py:2017-2022,2028`) | **Not fixed -- `totalItems` is the page size, not the collection size.** The sticky query is `.limit(50)` (`:2017-2018`), the remaining query is `.limit(50 - len(sticky_posts))` (`:2019-2021`), `posts = sticky_posts + remaining_posts` (`:2022`), and `"totalItems": len(posts)` (`:2028`). So a community with 4000 posts advertises an `OrderedCollection` of exactly 50, and there is no `next`/`first` page link anywhere in the document -- a remote instance paginating on that number sees a collection that claims to be complete at 50. Not fixed: correcting it changes what peers are told about every community on the instance, which is the line the spec draws for registration, and the honest fix is pagination rather than a different integer. | not fixed, registered only | reading-level: the two `.limit()` calls, the concatenation and the `len(posts)` read directly at the cited lines. The 50-item cap is not exercised by a test -- no test in `tests/test_ap_collections.py` seeds 51 posts -- so this is derived from reading, not measured |
| D174 | `community_moderators` (`app/utils.py:2889-2899`), reached from `community_moderators_route` (`app/activitypub/routes.py:2073`) | **Not fixed -- the moderators collection publishes a phantom moderator with no database row.** After running its `is_owner OR is_moderator` query, the function does `if community.user_id not in [mod.user_id for mod in mods]: mods.append(CommunityMember(user_id=community.user_id, is_owner=True, community_id=community.id))` (`:2897-2898`). The `CommunityMember` is **constructed and never added to the session** -- it has no `id`, it is not persisted, and it disappears at the end of the call. `community_moderators_route` then feeds `mod.user_id` from that list into a `User.id.in_(...)` lookup (`:2074`) and publishes the resulting actor's `public_url()`, so `/c/<name>/moderators` advertises a moderator that no `community_member` row backs. Contrast `community_members` (`app/activitypub/util.py:54-58`), which `community_followers` uses: a plain `SELECT COUNT(*)` over persisted rows, with no synthesis. Not fixed: the synthesis is in `app/utils.py`, outside the nine functions this sub-project was authorised to change, and it has callers throughout the application that may depend on it. | not fixed, out of scope | measured, and it **invalidated two of the spec's own tests**: `seed_local_community` always sets `user_id=1` (`seed_actors`' `communityowner`, for whom no `CommunityMember` row is ever created), so the synthesis fires on every seeded community and the moderators collection can **never** be empty through these factories. The brief's "a community with zero moderators" test was therefore impossible and its exact-`orderedItems` equality wrong; both were rewritten rather than forced, as `test_a_community_with_no_explicit_moderators_still_lists_its_owner` and `test_a_non_moderator_member_is_not_listed`. The latter's `not in` form still kills the `OR` filter's mutation while correctly accounting for the always-present phantom owner. Source read directly at `app/utils.py:2889-2899` |
| D175 | `community_followers` (`app/activitypub/routes.py:2103-2104`) and `feed_followers` (`:2863-2864`) vs. `user_followers` (`:2127-2135`) | **Not fixed -- two of the three followers collections report a real `totalItems` beside a permanently empty `items`; the third does not.** `community_followers` sets `"totalItems": community_members(community.id)` -- a genuine SQL `COUNT` -- next to `"items": []`, an unconditional literal with no code path anywhere in the function that could populate it. `feed_followers` does the same with `db.session.query(FeedMember).filter_by(feed_id=feed.id).count()`. `user_followers`, the third, populates its `items` with real follower URLs and additionally filters blocked and unaccepted follows (`:2120-2129`). Hiding follower lists is a defensible privacy choice; reporting a **non-zero count** beside an empty list is self-contradictory, because a consumer cannot distinguish "hidden" from "none". That two of three do it and one does not is the asymmetry, and it is not a `feed_followers` quirk. Not fixed: either direction (populate the items, or zero the count) changes what peers are told. | not fixed, registered only | measured: `test_the_community_followers_items_list_is_always_empty` and `test_the_feed_followers_items_list_is_always_empty` each drive `totalItems` to a non-zero value with a seeded membership row and assert `items == []` in the same response, so the pin is discriminating rather than vacuous; `test_a_users_followers_are_listed` shows the third endpoint populating its own `items`. **This endpoint pair is why the sub-project has nine endpoints and not eight** -- `community_followers` was missing from the spec entirely and was found by Task 1's reviewer, which is also what turned a `feed_followers` quirk into a two-of-three pattern |
| D176 | The four feed lookups (`app/activitypub/routes.py:2748`, `:2786`, `:2826`, `:2857`) vs. the community lookups (`:2015`, `:2046`, `:2071`, `:2097`) and the user lookup (`:2117`) | **Not fixed -- the feed lookups have neither a `banned` nor a `public` guard, and the four feed endpoints disagree with each other about whether a private feed's data is public.** All four community lookups and the one user lookup filter `banned=False`. All four feed lookups are `filter_by(name=actor.lower(), ap_id=None)` -- **no `banned`, no `public`** -- even though `Feed.banned` (`app/models.py:4117`) and `Feed.public` (`:4098`) both exist. `feed_outbox` (`:2756-2757`) and `feed_following` (`:2794-2795`) then check `feed.public` **after the fact** and `abort(403)`; `feed_moderators_route` and `feed_followers` never check it at all. So a non-public feed's follower count and moderator list are served to anyone, while the same feed's outbox and following collections are 403 -- and a *banned* feed is served by all four. Cross-reference **D158** and **D148**: this is the same systematic lookup-guard divergence, now on a fifth through eighth endpoint. Not fixed: adding the guards would 404 or 403 data that is currently served, a federation-visibility change of exactly the kind this campaign registers rather than decides. | not fixed, registered only | measured for the `public` half: `test_a_non_public_feed_outbox_is_403` and `test_a_non_public_feed_following_is_403` pin the two that check, and `test_a_non_public_feed_still_has_a_followers_collection` pins one that does not -- the same feed, opposite answers from two sibling endpoints. Reading-level for the `banned` half: no test drives a banned feed through any of the four; all nine lookup lines read directly |
| D177 | All nine functions | **Not fixed -- none of the nine checks `is_activitypub_request()`, so all nine serve ActivityPub JSON to a browser.** `app/activitypub/routes.py` calls `is_activitypub_request()` at ten sites (`:388`, `:394`, `:486`, `:524`, `:604`, `:2149`, `:2176`, `:2219`, `:2239`, `:2663`) -- the three actor-profile endpoints all branch on it (D164), and so do the four content-object endpoints (`post_ap`, `comment_ap`, `post_replies_ap`, `post_ap_context`) at `:2149,2176,2219,2239`. **Not one of those ten falls inside any of the nine collection endpoints' line ranges.** So `/c/books/outbox` in a web browser returns `application/activity+json` with a full `OrderedCollection` and no HTML alternative, where `/c/books` negotiates. Not fixed: adding negotiation to nine endpoints means deciding what the HTML answer *is* for each, which is a feature, not a fix. | not fixed, registered only | measured: the ten call sites enumerated by grep and each checked against the nine functions' line ranges -- independently, twice (sub-project 10's Task 1 implementer and its reviewer). **The ten line numbers above were re-measured at sub-project 11's Task 11 commit and two of them corrected there** (`:2237` -> `:2239`, `:2661` -> `:2663`): sub-project 11's Task 10 inserted two lines at `:2233` (`else: abort(400)` in `post_replies_ap`, registered as D188), shifting every site below that point by two. The original evidence note said the sites were re-checked "at this task's commit after Task 10's fixes shifted the file", which meant **sub-project 10's** Task 10 and read as freshly verified while being stale -- worse than an undated citation. Naming the sub-project is the fix; the finding's substance (that none of the nine collection endpoints negotiates) never depended on the numbers. It is also the premise the whole test suite rests on: `collection_get` sends **no** `Accept` header precisely because none of the nine reads one |
| D178 | `user_followers` (`app/activitypub/routes.py:2140`) vs. the other eight | **Not fixed -- only `user_followers` sets `Vary: Accept`, and it is the one endpoint whose body demonstrably does *not* vary by `Accept`.** `resp.headers.set('Vary', 'Accept')` appears once across the nine, at `:2140`. Since none of the nine negotiates at all (D177), the header is inert where it is present and, if any of them ever gained negotiation, absent everywhere it would then matter. The inversion of D161, which registered `feed_profile` as the one actor-profile endpoint *missing* the header it needs. Note `user_outbox` (`:506`) also sets it, but that endpoint is outside this sub-project's nine, so "only one of the nine" is correctly scoped. Not fixed: harmless as it stands, and correcting it in either direction is a header-policy decision across nine endpoints. | not fixed, registered only | measured: `test_the_followers_collection_sets_cache_and_vary` asserts the real observable value, which is `'Accept, Accept-Encoding'` and never bare `'Accept'` -- see harness fact 38. Reading-level for the absence at the other eight: every `resp.headers.set` line in all nine functions read directly |
| D179 | `community_featured` (`app/activitypub/routes.py:2048`) vs. `community_outbox` (`:2017-2021`) | **Not fixed -- the two endpoints disagree about a post under review, and publish opposite answers about the same row.** `community_outbox` filters `Post.status > POST_STATUS_REVIEWING` on **both** its queries; `community_featured` filters only `filter_by(community_id=..., sticky=True, deleted=False)` -- no status clause at all. So a sticky post still under moderator review is **excluded from the outbox and published in the featured collection**. Not fixed: whichever way it is reconciled, a post currently visible to peers becomes invisible or vice versa. | not fixed, registered only | measured, and the pairing is exact, verified byte-for-byte by a reviewer: `test_a_sticky_post_under_review_is_excluded` (outbox) and `test_a_featured_post_under_review_is_published_anyway` (featured) build the **identical** post -- same community helper, same author, same `ap_id`, `sticky=True`, `status=0` -- and hit the two endpoints for opposite results. "Same post, two endpoints, opposite answers" is exactly true here, not an overclaim |
| D180 | `community_featured` (`app/activitypub/routes.py:2061-2063`) vs. the other eight | **Not fixed -- `community_featured` sets no `Cache-Control` at all, and the eight that do set four different values with no evident rationale.** The eight: `community_outbox` `max-age=10` (`:2037`), `community_moderators_route` `120` (`:2088`), `community_followers` `10` (`:2108`), `user_followers` `15` (`:2139`), `feed_outbox` `5` (`:2775`), `feed_following` `10` (`:2815`), `feed_moderators_route` `10` (`:2844`), `feed_followers` `15` (`:2868`) -- i.e. one 5, four 10s, two 15s and one 120, for nine documents of the same kind. `community_featured` sets none, so its caching falls to whatever default the deployment applies. The `Cache-Control` half of D160, now on all nine of this slice's endpoints. Not fixed: picking a value for `community_featured` means picking values for all nine, which is a caching-policy decision this slice should not settle alone. | not fixed, registered only | measured: `test_the_featured_collection_sets_no_cache_control` asserts the absence directly, **and the absence was cross-checked against the global `after_request` hook** (`app/request_hooks.py:122-177`) to rule out the header being set globally and the defect being narrower than described -- that hook's `activity+json` branch sets `Cache-Control` only for `/static/` and `/bootstrap/static/` paths, and its `/api/` and `text/html` branches never match this request. Read line by line by a reviewer, independently of the report. The eight values read directly at the cited lines |
| D181 | `user_followers` (`app/activitypub/routes.py:2118`) | **Not fixed -- a local user nobody remote has ever followed has no followers collection at all.** The guard is `if user is not None and user.ap_followers_url:`, so a null `ap_followers_url` 404s rather than returning an empty collection. `User.ap_followers_url` (`app/models.py:1070`) has no declared default, and **local registration never sets it**: `finalize_user_setup` (`app/utils.py:2902-2915`) populates `ap_profile_id`, `ap_public_url` and `ap_inbox_url` and stops there. The column is filled in **lazily**, at `app/activitypub/routes.py:1033-1034`, the first time an inbound remote `Follow` is accepted for that user. So `/u/<name>/followers` 404s for every local account until someone remote follows it, and then starts working -- a state transition no peer can predict. Compare `Community`, which gets `ap_followers_url` at creation (`app/community/routes.py:126`). Not fixed: the honest fix is either populating the column at registration (a data migration for existing users) or dropping the guard (which changes the `id` the document reports), and neither is confined to these nine functions. | not fixed, registered only | measured: `test_a_user_without_a_followers_url_is_404` pins the 404. Reading-level for the cause: `app/utils.py:2902-2915`, `app/activitypub/routes.py:1033-1034` and `app/community/routes.py:126` read directly, and a grep for every `ap_followers_url` assignment in `app/` returns no local-user write outside the lazy one |
| D182 | `user_followers`' block filter (`app/activitypub/routes.py:2119-2123`) | **Not fixed -- the filter runs the opposite direction from the comment above it, and the two are materially different privacy behaviours.** The comment (`:2119`) says "Get all followers, except those that are blocked by user". The query joins `UserFollower` as `User.id == UserFollower.remote_user_id` (`:2120`), so `User.id` is the **follower**; the outer join is `(User.id == UserBlock.blocker_id) & (UserFollower.local_user_id == UserBlock.blocked_id)` (`:2121-2122`), so the row it matches -- and therefore excludes -- is one where the **follower blocked the account owner**, not one where the owner blocked the follower. One behaviour hides people you do not want to see; the other hides people who do not want to see you. Not fixed: which one is intended is a privacy-policy question, and the code and its comment cannot both be right. At minimum the comment is wrong. | not fixed, registered only | **measured, and the discriminating check was done**: `test_a_blocked_follower_is_not_listed` seeds `UserBlock(blocker=follower, blocked=owner)` and asserts `totalItems == 0`. Had the row been seeded the other way round, the join's `ON` condition would not match it, `UserBlock.id` would stay `NULL`, the follower would **not** be excluded, and `totalItems` would come back 1 -- failing the assertion. So the test is genuinely sensitive to direction rather than passing either way. The controller predicted this from reading before the task ran; it is now established by a passing test |
| D183 | `community_moderators_route` (`app/activitypub/routes.py:2084`) vs. `feed_moderators_route` (`:2840`) | **Not fixed -- the two moderators collections publish two different URL forms for the same kind of thing.** `community_moderators_route` appends `moderator.public_url()`, which returns the row's stored `ap_public_url` and falls back to a constructed `{SERVER_URL}/u/{user_name}`. `feed_moderators_route` appends `moderator.ap_profile_id`, the raw column, with no fallback -- so a local user whose `ap_profile_id` has not been populated contributes a literal `null` to the feed moderators list where the community collection would contribute a working URL. Not fixed: changing either one changes the actor URIs peers store for moderators. | not fixed, registered only | reading-level: `:2084` and `:2840` read directly. `test_a_feed_moderators_collection_lists_its_owner` **could not** discriminate this as first written, and the reason is worth keeping: `make_user(local=True)` (`tests/factories.py:58-60`) leaves `ap_profile_id`, `ap_public_url` and `ap_id` all `None`, so its `orderedItems == [owner.ap_profile_id]` compared `[None] == [None]` and would have passed equally against a `public_url()` or `ap_id` regression. Fixed in `da54f03f`: the owner is now given `ap_profile_id` and `ap_public_url` at different values, the test asserts the literal `ap_profile_id` and adds an explicit `!= [owner.ap_public_url]`, and the discrimination is mutation-proved -- rendering `public_url()` in `feed_moderators_route` fails that test alone, on the assertion rather than by crashing. So the *production* divergence below is registered and unfixed, but the suite now detects a regression in either direction. See harness fact 50 |
| D184 | `feed_moderators_route` (`app/activitypub/routes.py:2834`) and `feed_followers` (`:2861`) | **Not fixed -- two more endpoints whose `id` echoes the caller's casing while the lookup resolves case-insensitively.** Both build `id` from the raw `actor` path segment (`f"{SERVER_URL}/f/{actor}/moderators"`, `f'{SERVER_URL}/f/{actor}/followers'`) while resolving the feed with `filter_by(name=actor.lower(), ...)`. So `GET /f/NEWS/followers` resolves the feed named `news` and returns `id` `https://.../f/NEWS/followers`, which is not that collection's canonical URL. The same class as **D159**, on a fourth and fifth endpoint. The community and user collections escape it for a reason worth stating rather than assuming: their lookups match `name=actor` / `user_name=actor` **exactly**, with no `.lower()`, so a mis-cased request 404s instead of resolving; `feed_outbox` and `feed_following` escape it because they take `id` from the stored `ap_outbox_url`/`ap_following_url` columns instead of the path. Not fixed: it changes the `id` peers store for two collections. | not fixed, registered only | reading-level: the two `id` constructions, the four feed lookups' `.lower()`, and the five community/user lookups' exact matches read directly side by side. No test in `tests/test_ap_collections.py` requests a mis-cased feed actor, so this is derived from reading, not measured |
| D185 | `community_outbox` (`app/activitypub/routes.py:2026,2029`) vs. `feed_outbox` (`:2769,2771`) | **Not fixed -- two endpoints named `outbox` declare two different collection types.** `community_outbox` is `"type": "OrderedCollection"` with `"orderedItems"`; `feed_outbox` is `"type": "Collection"` with `"items"`, even though its own query is `.order_by(desc(FeedItem.id))` (`:2760`) and therefore *is* ordered. `feed_outbox`'s comment explains the intent -- it is deliberately "the same as the /following collection" -- so the divergence is understood rather than accidental, but a peer consuming `outbox` by name gets two different document shapes from the same instance. Registered as the smallest of this set, and only because a consumer written against one will not read the other. Not fixed: changing a declared ActivityStreams type is a wire-format change. | not fixed, registered only, informational | reading-level: all four lines and the `order_by` read directly |
| D186 | `community_outbox`'s two queries (`app/activitypub/routes.py:2017-2021`) and `user_followers`' lookup (`:2117`) -- a **test-suite** finding, not a production defect | **Not a production defect, and recorded so nobody re-files it as one.** Four separate filter clauses were unkillable by mutation when this sub-project began, and each needed a test written for it. (a) `community_outbox` duplicates **three** filters across its sticky and remaining queries -- `Post.deleted == False`, `Post.status > POST_STATUS_REVIEWING` and `Post.community_id == community.id` -- and the **sticky** copy of all three was enforced by no test: every test that cared about *filtering* used a default (non-sticky) post, and every test that cared about *stickiness* used a clean one, so the two concerns never met in a single fixture. All three sticky-side clauses could have been deleted outright with the suite green, meaning a deleted or under-review sticky post would be published to every federating peer uncaught. Closed by `test_a_deleted_sticky_post_is_excluded`, `test_a_sticky_post_under_review_is_excluded` and `test_a_sticky_post_in_another_community_is_excluded`. (b) `user_followers`' `banned=False` clause had no banned-user case at all; closed by `test_a_banned_user_has_no_followers_collection`. **The general rule, and it generalises past this sub-project**: a filter duplicated across two call sites has two ways to be wrong, and a suite can enforce one while leaving the other free -- mutating both sites together shows a kill and hides exactly this. See harness fact 42. | closed by added tests; no production change | measured: after the three added tests, **all six** single-site mutations on `community_outbox`'s two queries (two sites x three clauses) die, each a distinct **sole assertion-kill**, verified one site at a time; the `banned=False` mutation likewise. Before them, three of the six survived -- all three sticky-side. Each attribution was traced against source by a reviewer rather than restated from the report |

**Two shapes worth carrying forward from this sub-project's rulings, both now in
`tests/README.md` as facts 43-46:** a crash-to-404 inversion can only ever be
mutation-killed by a **crash-kill**, because the mutant raises before the
assertion runs -- an assertion-kill is structurally impossible for that shape,
not merely unachieved; and a "do not fix X" constraint scopes the **code**, not
the prose about it -- a docstring made false by a sibling task is a defect the
constraint never covered, which is why `0479a62f` exists.

## Sub-project 11: the ActivityPub content-object and activity-log endpoints

`docs/superpowers/specs/2026-09-03-coverage-content-objects-11-design.md` and
`docs/superpowers/plans/2026-09-03-coverage-content-objects-11.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-03-coverage-content-objects-11/`, not committed), on
branch `blentz`. Eleven tasks brought six contiguous functions in
`app/activitypub/routes.py` under test -- `comment_ap` (`:2146-2166`), `post_ap`
(`:2174-2209`), `post_replies_ap` (`:2217-2234`), `post_ap_context`
(`:2237-2262`), `activities_json` (`:2265-2280`) and `activity_result`
(`:2285-2294`) -- and fixed one defect in one commit (`e1951de5`), under this
sub-project's own bounded, explicit authorisation; this task (11) closes the
sub-project out with the findings register, the test-harness log, and the
coverage floor. `app/activitypub/routes.py` measures **90.8059% blended**
(1681/1832 statements, 798/898 branches) after this sub-project, up from
86.2221% (1593/1831 statements, 760/898 branches, sub-project 10's own ending
figure). Full suite after this sub-project: **3328 passed, 3 skipped, 6 subtests
passed**, in 192.56s. Tests live in `tests/test_ap_content_objects.py` (43 test
functions, none parametrized: 42 added across Tasks 1-9, one of those inverted in
place by Task 10, and a forty-third -- D199's pin -- added by the final review's
fix wave. That pin postdates the 90.8059% above, which was measured at 42 test
functions; it covers an already-covered path, so it does not move the figure).
The coverage figure above is the controller's single
authoritative module-level measurement taken after Task 10; **no per-function
residual breakdown was re-measured at Task 11**, so this section claims the
module figure and not "zero uncovered statements" for any individual function.

Like 5c through 10 before it, this sub-project carried a **narrow, explicitly
authorised exception** to the campaign's report-don't-fix rule: Task 10 was
authorised to fix the one remotely-reachable HTTP 500 that Task 6's pin had
established, confined to `post_replies_ap`. `git diff --stat app/` is non-empty
for exactly that one commit (`e1951de5`); every other task's `git diff --stat
app/` is empty.

The slice exists because the four content-object endpoints are four
near-identical answers to one question -- *serve this content object as
ActivityPub JSON* -- and, as in sub-projects 8, 9 and 10, the defects live in
what they do **differently**. `post_ap` checks `local_only`, `private`, `status`
and the instance block before serving a post. `post_replies_ap`, which begins
eight lines after `post_ap` ends (`:2209` -> `:2217`), enumerates that same
post's replies behind no guard at all. That
contrast is D189, and it is the shape the whole slice was built to expose.

**Four of the spec's and plan's predictions were falsified, and each
falsification is worth more than the prediction was.**

1. **The plan's per-task "Expected: N passed" counts were wrong twice, for two
   different reasons.** The pre-flight scan caught the first (every count was
   one too high; Task 1 defines three tests, not four) and corrected it in
   `031d046d` before dispatch. The second was introduced *by the controller
   mid-flight*: the Task 7 fix-round ruling ordered a seventh test to close a
   combinatorial gap, which silently invalidated every later task's running
   total while leaving each one internally consistent. Task 9's implementer
   caught it. The true sequence is 3, 9, 15, 19, 23, 29, 35 -> 36 after the fix
   round, 39, 42. Now `tests/README.md` fact 55.
2. **The first coverage run's figure was a stale file, not a result.** The run
   hit the 600s session-timeout wall at 2316 of ~3290 tests and never wrote
   `scratch_full_cov.json`, so the figures read out of it (1593/1831, 760/898,
   86.2221%) were **byte-identical to sub-project 10's** -- which is impossible
   after 42 new tests closing 89 previously-uncovered statements, and is the
   tell. A controller who read the number without checking the artefact's mtime
   would have raised the floor against a measurement predating the entire
   sub-project, and it would have passed the floor check. Cause was podman stack
   degradation, the pattern this campaign debugged earlier; `./run_tests.sh
   --down` and a re-run on a fresh stack took 192.56s against the previous run's
   600s wall, on identical code. Now `tests/README.md` fact 54.
3. **The spec predicted 89 uncovered statements would close; 87 did** (238 ->
   151 missing). Not a shortfall: two of the six functions' statements were
   already partly covered by other suites before this sub-project, and
   `num_statements` rose 1831 -> 1832 because Task 10's `abort(400)` is a new
   statement. The spec's headline prediction -- "toward **90%**" -- was hit at
   90.8059%.
4. **The spec's mutation discipline assumed every conjunct of every guard was
   killable; one was not.** `post_ap_context`'s reply query filters
   `post_id=post_id, deleted=False` (`:2244`), and dropping `post_id=post_id`
   killed nothing, because every fixture in the file seeded its replies under a
   single post. Registered as D197 and closed in the same sub-project.

### 1. The most serious finding in this slice, registered not fixed -- D187

Registered first and on its own, not buried in the list below, because it is the
only finding here that hands a remote caller information about this instance's
internals rather than merely serving content it should have withheld.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D187 | `activity_result` (`app/activitypub/routes.py:2285-2294`, the failure arm at `:2292`) | **Not fixed -- the endpoint returns this instance's internal exception text to any unauthenticated caller.** `activity_result` has no authentication, no signature check and no `is_activitypub_request()` gate: it looks up `ActivityPubLog` by `activity_id=f'https://{id}'` (`:2287`), and on `activity.result != 'success'` returns `jsonify({'error': activity.result, 'message': activity.exception_message})` (`:2292`). `ActivityPubLog.exception_message` (`app/models.py:3694`) is populated **from caught exceptions**: `log_incoming_ap`'s `message` parameter is assigned straight to it (`app/activitypub/util.py:4534,4541`) and three of that function's callers in this same file pass `str(e)` verbatim -- `'Precheck failed: ' + str(e)` (`:689`), `'Could not verify LD signature: ' + str(e)` (`:723`), `'Could not verify HTTP signature: ' + str(e)` (`:737`) -- while `app/activitypub/signature.py` writes peer HTTP status codes and `'could not send:' + str(e)` into the same column (`:111,118,121,145`). **The activity id is not a secret the caller must guess: it is the id the *remote* instance itself chose and sent, so the peer that triggered the failure already knows exactly which URL to fetch.** So a peer whose activity blew up here can read back this instance's Python exception text, including file paths, SQL constraint names and library internals. Not fixed because the right replacement -- a generic error string, a logged reference the operator can correlate, or nothing at all -- is a decision about what peers are *told*, which is the line this campaign draws between fixing and registering. | not fixed, registered only; **the most serious finding in this slice** | measured: `test_a_failed_activity_result_discloses_the_internal_exception_message` (`tests/test_ap_content_objects.py`) seeds `result='failure'` and an `exception_message` reading `IntegrityError at app/activitypub/util.py:1214: duplicate key value violates unique constraint "user_ap_id_key"`, requests `GET /activity_result/peer.example/activities/announce/boom` unauthenticated, and asserts 200 plus `error == 'failure'` plus two substrings of that internal message. The two-substring assertion is deliberate: a generic-message fix breaks the pin loudly rather than silently, which is what a registered-not-fixed finding needs. The population chain was then re-read at the four writer sites cited above |

### 2. One defect fixed, test-first with a mutation-proved test -- D188

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D188 | `post_replies_ap` (`app/activitypub/routes.py:2217-2234`) | **FIXED, commit `e1951de5`. The FOURTH instance of the crash class sub-project 10 fixed three times as D167-D169.** The whole body sat inside `if (request.method == 'GET' or request.method == 'HEAD') and is_activitypub_request():` (`:2219`) with **no `else`**. Any request without an ActivityPub `Accept` header -- i.e. every browser -- fell off the end of the view, the view returned `None`, and Flask raised `TypeError: The view function for 'activitypub.post_replies_ap' did not return a valid response` (`flask/app.py:1212`). Remotely reachable by anyone who can click a link. The fix is two lines, `else: abort(400)` (`:2233-2234`), **copied from `post_ap_context`'s own else at `:2261-2262` rather than a fifth spelling invented** -- `comment_ap` and `post_ap` both delegate to an HTML renderer instead, which is a richer answer, but adopting it here would mean choosing an HTML view for a replies collection that has none, a larger change than this slice authorised. Blast radius two lines: the AP arm untouched, the dead `HEAD` clause (D192) untouched, the route still `methods=['GET']`. | **FIXED** -- test-first, pinned in Task 6 (`87633159`), inverted and fixed in Task 10 (`e1951de5`) | measured, with the pre-fix failure WITNESSED and quoted, not assumed: Task 6's pin asserted `pytest.raises(TypeError, match='did not return a valid response')` against the unfixed route and passed; Task 10 inverted it to `test_a_browser_request_for_post_replies_is_400` asserting `status_code == 400`. Mutation: removing the new `else` gives 1 failed, 41 passed, killing exactly that one test -- a **crash-kill**, and structurally so, since the mutant raises before the status assertion is reached (harness fact 44) |

### 3. Eight items registered, not fixed -- D189-D196

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D189 | `post_replies_ap` (`app/activitypub/routes.py:2218-2234`) vs. `post_ap` (`:2175-2209`) | **Not fixed -- `post_replies_ap` has no visibility guard whatsoever, where `post_ap` has four.** `post_ap` checks `post.community.local_only or post.community.private or post.status < POST_STATUS_PUBLISHED` (`:2179`) and then the instance block (`:2181-2182`) before serving a post. `post_replies_ap` does `Post.query.get_or_404(post_id)` (`:2220`) and goes straight to `post_replies_for_ap(post.id)` (`:2223`): no `deleted`, no `local_only`, no `private`, no `status`, no instance-block check. So `GET /post/<id>/replies` enumerates the reply bodies of a post that `GET /post/<id>` would have refused with 403 -- including a local-only community's, a private community's, and one still under review. The reply URLs it returns are the same ones `post_ap_context` gates behind `post.deleted` (D190). Not fixed: adding four guards to this endpoint is a federation-visibility decision about a collection nothing in this slice measured the consumers of, and the campaign's authorisation here covered the crash only. **"where `post_ap` has four" counts the four guards `post_ap` does have; it is not a claim that `post_ap` is fully guarded -- see D199, which registers `post_ap`'s own missing `deleted` guard.** | not fixed, registered only | measured, three pins, each overriding its trigger column explicitly rather than resting on a factory default: `test_post_replies_are_served_for_a_local_only_community`, `test_post_replies_are_served_for_an_unpublished_post` and `test_post_replies_are_served_for_a_deleted_post` (`tests/test_ap_content_objects.py`) each seed the condition and assert **200**, so each fails loudly the moment a guard is added |
| D190 | `post_ap_context` (`app/activitypub/routes.py:2238-2262`) vs. `post_ap` (`:2179`) | **Not fixed -- `post_ap_context` checks only `deleted`, so a local-only community's post titles and reply URIs are served to any caller.** Its sole guard is `if post.deleted: abort(404)` (`:2241-2242`). It then publishes `post.ap_id` and up to 2000 reply `ap_id`s (`:2244-2247`), `post.title` as `name` (`:2252`), and `post.community.profile_id()` as both `attributedTo` and `audience` (`:2253-2254`) -- with no `local_only`, no `private`, no `status` check. A post in a local-only community, or one still under review, leaks its title, its canonical URI, its community and every non-deleted reply's URI through `/post/<id>/context` while `/post/<id>` returns 403. This is the same class as D189 on a second endpoint and with one guard present rather than none. Not fixed for the same reason as D189. **"while `/post/<id>` returns 403" is true of the `local_only`/`private`/`status` cases this entry discusses and NOT of the `deleted` case: `post_ap` has no `deleted` guard at all, so a deleted post is served whole from `/post/<id>` while `/post/<id>/context` 404s -- see D199.** | not fixed, registered only | reading-level plus measured: the guard set at `:2241-2242` read directly against `post_ap`'s at `:2179`; `test_a_deleted_post_has_no_context` pins the one guard that IS present (404), and the reply-listing tests confirm the document's contents are exactly the fields enumerated above |
| D191 | `comment_ap` (`app/activitypub/routes.py:2147-2166`) vs. `post_ap` (`:2178,2203`) | **Not fixed -- `comment_ap` never checks `reply.deleted`, and never checks whether the reply is local.** It checks `local_only`/`private` (`:2150-2151`) and the instance block (`:2152-2153`), then serves `comment_model_to_json(reply)` (`:2154`) unconditionally. Two gaps follow. First, a deleted reply is served in full: `post_ap_context` filters `deleted=False` on the very same rows (`:2244`), so the reply this instance withholds from a post's context is served whole from `/comment/<id>`. Second, `post_ap` calls `post.is_local()` (`:2178`) and redirects a remote post **301 to its `ap_id`** (`:2203`) precisely so the origin instance stays authoritative for its own content; `comment_ap` has no such branch and re-serves a remote reply's JSON as though this instance were authoritative for it. Not fixed: the `deleted` half is a one-line guard but changes what a peer sees for content it may already hold, and the `is_local` half means adding a redirect arm to an endpoint whose remote-serving behaviour other instances may depend on -- both federation-behaviour decisions, not fixes. | not fixed, registered only | reading-level: `comment_ap`'s full body read directly against `post_ap`'s `is_local()`/301 pair and against `post_ap_context`'s `deleted=False` filter. No test in this sub-project drives a deleted or remote reply through `/comment/<id>` to observe the divergence, so this is derived from reading, not measured -- said plainly rather than implied |
| D192 | `post_replies_ap` (`app/activitypub/routes.py:2217,2219,2225-2226`) and `post_ap_context` (`:2237,2239,2248-2249`) | **Not fixed -- two dead `HEAD` branches. Either the route list or the branch is wrong, and nothing in the code says which.** Both routes are registered `methods=['GET']` (`:2217`, `:2237`), so Flask never dispatches a `HEAD` to either -- yet both bodies open with `if (request.method == 'GET' or request.method == 'HEAD') and is_activitypub_request():` (`:2219`, `:2239`) and both carry an `else: replies_collection = {}` arm (`:2225-2226`, `:2248-2249`) that exists only to answer a `HEAD`. Neither arm can execute. Contrast `comment_ap` (`methods=['GET', 'HEAD']`, `:2146`) and `post_ap` (`methods=['GET', 'HEAD', 'POST']`, `:2174`), whose equivalent branches are live and are covered by `test_a_head_request_for_a_post_returns_an_empty_activitypub_body`. Not fixed: adding `'HEAD'` to the two route lists and deleting the two dead arms are opposite fixes with opposite federation consequences, and choosing between them is a decision about the endpoints' contract rather than a defect repair. | not fixed, registered only | reading-level plus measured: the two route decorators and the two `else` arms read directly; the branches are unreachable by construction, so no test can cover them and their statements are among the module's residual uncovered lines |
| D193 | `find_instance_id` (`app/activitypub/util.py:2064-2086`), reached from `comment_ap` (`app/activitypub/routes.py:2152`) and `post_ap` (`:2181`) | **Not fixed -- an unauthenticated GET writes and commits a row to the `instance` table.** Both endpoints evaluate `find_instance_id(requestor_domain())` on every ActivityPub GET. When the domain is not already known, `find_instance_id` constructs `Instance(domain=server, software='unknown', inbox=f'https://{server}/inbox', created_at=utcnow())` (`:2074`), then `db.session.add` and `db.session.commit` (`:2077-2078`), and spawns `new_instance_profile` (`:2084`). So a read endpoint has a write side effect, and any caller can create arbitrary `instance` rows -- and arbitrary background profile fetches against hosts of their choosing -- simply by varying the `+URL` suffix in their `User-Agent`. Larger than these six functions (the same helper is called from the inbox path and elsewhere), so registered rather than fixed; but it is reached from two of them, and this sub-project's tests demonstrate it. | not fixed, registered only | reading-level plus measured: `find_instance_id`'s add/commit read directly, and the two call sites confirmed; measured indirectly by Task 2's discipline, where every domain named in a `user_agent=` argument was given its own `make_instance()` in the same test **precisely so the auto-create would not fire** and the test would not measure the wrong branch -- the workaround is the evidence |
| D194 | The instance-block guard in `comment_ap` (`app/activitypub/routes.py:2152-2153`) and `post_ap` (`:2181-2182`) | **Not fixed -- the guard is inert for every client that does not volunteer a `+`-style `User-Agent`, and nothing documents that.** The chain is three links, each individually reasonable: `requestor_domain()` (`app/utils.py:5736-5743`) returns `''` unless the `User-Agent` string contains a `'+'` (`:5739`); `find_instance_id('')` returns `None` at its own `if not server:` (`app/activitypub/util.py:2065-2066`); and `User.has_blocked_instance(None)` returns `False` at `if instance_id is None:` (`app/models.py:1467-1469`). So the 401 arm fires only for a caller who identifies its origin voluntarily. A blocked instance that omits the suffix, or sends a browser-shaped agent, is served the content anyway. Whether volunteer-identification is sufficient is a federation-policy question -- most well-behaved server software does send it -- and this entry does not claim the guard should reject more. **The finding is that a security-shaped guard silently no-ops on a header the caller controls, and no comment or docstring at either call site says so.** Not fixed: strengthening it means picking a different identification source (HTTP signature keyId, say), which is a design decision. | not fixed, registered only | reading-level, all three links read directly and hand-traced; and measured in the positive direction -- `test_a_comment_is_401_when_the_author_has_blocked_the_requesting_instance` and `test_a_post_is_401_when_the_author_has_blocked_the_requesting_instance` reach the 401 only by sending `'Test (+https://blocked.example)'`, whose parse (split on `'+'`, take the last part, strip `')'`, `furl().host`) yields `blocked.example`. Now also `tests/README.md` fact 51 |
| D195 | The four content-object endpoints' `Cache-Control` headers (`app/activitypub/routes.py:2161`, `:2190`, `:2231`, `:2259`) plus `activities_json` (`:2279`) | **Not fixed -- four documents of the same kind carry three different max-ages, with no evident rationale.** `comment_ap` sets `public, max-age=120` (`:2161`), `post_ap` `public, max-age=120` (`:2190`), `post_replies_ap` `public, max-age=15` (`:2231`), `post_ap_context` `public, max-age=15` (`:2259`); `activities_json` sets `public, max-age=2400` (`:2279`) and is additionally `@cache.cached(timeout=2400)` (`:2266`). **This is the same unexplained spread D180 registered across the nine collection endpoints, recurring on a second family of endpoints in the same file** -- there the eight that set the header set four different values and a ninth set none. A post and its own replies collection are cached for eight times as long as each other, so a shared cache can serve a fresh post beside a stale reply list. Not fixed: the campaign has no basis for choosing the right value, and D180's reasoning applies unchanged. | not fixed, registered only | reading-level: all five `Cache-Control` lines read directly; measured too -- each of the five is asserted by full string equality on a success path in `tests/test_ap_content_objects.py`, so any future normalisation fails loudly rather than silently |
| D196 | `activities_json`'s not-found path (`app/activitypub/routes.py:2277-2280`) | **Not fixed -- the 404 is served with `Cache-Control: public, max-age=2400`.** The header is set at `:2279`, *after* the `if activity: ... else: resp = make_response('', 404)` (`:2270-2278`), so it lands on both arms. A miss is therefore cached by shared caches and by the peer for forty minutes. That matters because this is the endpoint a remote instance polls to find out whether an activity it just sent was recorded: a request that arrives fractionally before the row is written is answered 404, and that 404 is then held for 2400 seconds. Contrast the found path, whose freshness genuinely can be long-lived because a logged activity is immutable. Not fixed: whether a miss here should be `no-store`, short-lived or unchanged depends on the polling contract with peers, which is a federation decision. | not fixed, registered only | measured: `test_an_unlogged_activity_is_404_with_a_cache_header` requests an activity id with no `ActivityPubLog` row and asserts both `status_code == 404` and the full `Cache-Control` string, so the coupling is pinned rather than inferred |

### 4. Two test-suite findings, not production defects -- D197-D198

Recorded so nobody re-files either as a production defect, and because the first
is the second instance in this campaign of a mutation-coverage failure mode that
no amount of reading the production code can reveal.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D197 | `post_ap_context`'s reply query (`app/activitypub/routes.py:2244`) -- a **test-suite** finding | **Not a production defect. The `post_id=post_id` clause of `PostReply.query.filter_by(post_id=post_id, deleted=False)` was enforced by no test in the suite until this sub-project's Task 7 fix round, and the cause was a COMBINATORIAL GAP, the second of the three causes this campaign has catalogued.** Dropping `post_id=post_id` left every one of the file's 35 tests green: every fixture seeded its replies under a single post, so filtering on `deleted=False` alone returned the identical set and the mutation changed nothing observable. Had the clause actually been dropped in production, every post's `/context` would have listed every non-deleted reply on the instance. **CLOSED in the same sub-project** (`c763e116`): one purely additive test, `test_a_post_context_omits_another_posts_replies`, seeds a **second** post with its own reply and creates the contrast the fixtures lacked. The two clauses are now independently enforced -- dropping `deleted=False` kills only `test_a_post_context_omits_deleted_replies`, dropping `post_id=post_id` kills only the new test, each an assertion failure. The remedy for a combinatorial gap is a fixture that creates the missing contrast, not a stronger assertion on the existing one. Cross-reference **D186**, which registered four unkillable filter clauses in sub-project 10 for the neighbouring cause. | not a production defect; test-suite gap, **closed** | measured: the surviving mutant was reported honestly by Task 7's implementer rather than a kill manufactured, and both mutations were re-run after the fix. The attribution is clean in both directions -- the deleted-replies fixture is immune to the `post_id` mutation (no other post exists in its transaction) and the second-post fixture is immune to the `deleted` mutation (both its replies are undeleted) |
| D198 | `test_a_comment_is_401_when_the_author_has_blocked_the_requesting_instance` and its `post_ap` twin (`tests/test_ap_content_objects.py`) -- a **test-suite** finding | **Not a defect, and recorded so it is not re-filed as one.** Both 401 tests depend on the *visibility* guard above them not firing, and that non-triggering state rests on two **implicit column defaults** (`Community.private`, `Post.status`) where the three 403 tests in the same file set every guard value explicitly. Reviewed and judged acceptable on a stated ground rather than waved through: the tests assert **401**, so if either default flipped they would fail loudly (`401 != 403`) rather than pass silently -- which is the property that distinguishes an acceptable implicit default from a vacuous assertion (harness fact 50, where the failure mode was silent). The asymmetry with the sibling 403 tests is real and is the only reason this is written down. | not a defect; recorded for completeness | reading-level: the two 401 tests' seeding read directly against the three 403 tests' in the same file, and the failure direction hand-traced -- a flipped default routes the request to `abort(403)` before the block guard, which the tests' own `status_code == 401` assertion catches |

### 5. One more defect found by this fix wave's whole-sub-project review -- D199

The fourth and last cell of the slice's `deleted`-guard row, and the only one no
test or register entry disposed of when the sub-project's report was written.
Registered here and pinned by one purely additive test -- the file's
forty-third, where the section opening above counts the 42 the sub-project's
own tasks wrote. No production code changed in this fix wave (`git diff app/`
empty).

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D199 | `post_ap` (`app/activitypub/routes.py:2175-2209`) vs. `post_ap_context` (`:2241-2242`) | **Not fixed -- `post_ap` has no `deleted` guard, so a soft-deleted post's full `Page` JSON is served to any ActivityPub caller.** The word `deleted` does not appear anywhere in `post_ap`'s body: its guard set is `local_only or private or status < POST_STATUS_PUBLISHED` (`:2179`) and the instance block (`:2181-2182`), and nothing else. `post_ap_context`, serving the same row's context, aborts 404 on `if post.deleted:` (`:2241-2242`) -- so for one and the same post `GET /post/<id>/context` is 404 while `GET /post/<id>` returns the post's complete rendered `Page`, title, body, media and all. That is the sharper direction of the same asymmetry D189 and D191 record: **the two endpoints that leak a deleted post are the two that carry the most content**, `post_ap` (the post itself) and `comment_ap` (the reply itself), while the two that publish only URIs are the ones with -- or, for `post_replies_ap`, without -- the guard. Not fixed for the same reason as D189-D191: adding it changes what peers are shown for content they may already hold, a federation-visibility decision rather than a defect repair. **Cross-reference D189 and D190, whose wording predates this entry:** D189's "where `post_ap` has four" counts the four guards `post_ap` *does* have and should not be read as saying it is fully guarded, and D190's "while `/post/<id>` returns 403" is true of the `local_only`/`private`/`status` cases it is discussing and **not** of the `deleted` case, which is this entry. | not fixed, registered only | measured: `test_a_deleted_post_is_still_served_as_activitypub_json` (`tests/test_ap_content_objects.py`) sets `post.deleted = True` **explicitly** -- `make_post` sets `deleted=False`, so a default-resting test would assert nothing -- and asserts 200, `application/activity+json`, `type == 'Page'` and that `post_to_page` was called with the post. Mutation-proved in both directions: inserting `if post.deleted: abort(404)` into `post_ap` gives 1 failed, 42 passed, killing exactly this test on `assert 404 == 200`, an assertion kill; the mutation was then reverted and `git diff app/activitypub/routes.py` confirmed empty |

**Three shapes worth carrying forward from this sub-project's rulings, now in
`tests/README.md` as facts 51-55:** an instance-block guard reached through
`requestor_domain()` is unreachable without a `+`-style `User-Agent`, and a test
that omits one measures the wrong branch while looking correct; a coverage
figure identical to the previous sub-project's is a **stale-file symptom, not a
result**, so check the artefact's mtime and never trust a figure from a run that
printed no completion line; and a running total in a plan is invalidated by any
mid-flight ruling that adds or removes a test, silently -- every later number
stays internally consistent while being wrong, and the controller who orders the
addition owns correcting the counts downstream of it.

## Sub-project 12: the moderation and ban-removal cluster

`docs/superpowers/specs/2026-09-03-coverage-moderation-12-design.md` and
`docs/superpowers/plans/2026-09-03-coverage-moderation-12.md` (design and plan;
the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-03-coverage-moderation-12/`, not committed), on branch
`blentz`. Eleven tasks brought six functions in `app/activitypub/util.py` under
test -- `delete_post_or_comment` (`:2204-2265`), `restore_post_or_comment`
(`:2268-2306`), `site_ban_remove_data` (`:2309-2346`),
`community_ban_remove_data` (`:2349-2378`), `ban_user` (`:2412-2507`) and
`unban_user` (`:2510-2569`) -- and fixed one defect in one commit (`25de721d`),
under this sub-project's own bounded, explicit authorisation; this task (11)
closes the sub-project out with the findings register, the test-harness log, and
the coverage floor. `app/activitypub/util.py` measures **55.1080% blended**
(1639/2854 statements, 810/1590 branches) after this sub-project, up from
48.9424%. Full suite after this sub-project: **3365 passed, 3 skipped, 6
subtests passed**, in 227.31s. Tests live in `tests/test_ap_moderation.py` (36
test functions, none parametrized: 33 added across Tasks 1-9 plus three added by
two mid-flight fix rounds, and one of the 36 inverted in place by Task 10). The
coverage figure above is the controller's single authoritative module-level
measurement taken after Task 10 on a freshly torn-down stack; **no per-function
residual breakdown was re-measured at Task 11**, so this section claims the
module figure and not "zero uncovered statements" for any individual function.

Like 5c through 11 before it, this sub-project carried a **narrow, explicitly
authorised exception** to the campaign's report-don't-fix rule: Task 10 was
authorised to fix the one silently-non-persisting counter write Task 6's pin had
established, confined to `site_ban_remove_data`. `git diff --stat app/` is
non-empty for exactly that one commit (`25de721d`), and its production diff is
exactly one line; every other task's `git diff --stat app/` is empty.

The slice exists because these six functions are three do/undo pairs, and -- as
in sub-projects 8 through 11 -- the defects live in what the halves of a pair do
**differently**. `delete_post_or_comment`'s `PostReply` branch decrements four
counters; `restore_post_or_comment`'s increments two. That contrast is D200, it
is the shape the whole slice was built to expose, and it is the only finding here
that no amount of reading either function alone would have settled: a
single-direction test asserting that restore leaves a counter alone proves
nothing unless the delete moved it.

**Three of the spec's and plan's claims were falsified, and each falsification
is worth more than the claim was.**

1. **The spec claimed `site_ban_remove_data` purges the CDN while
   `community_ban_remove_data` does not.** `File.delete_from_disk`'s signature is
   `def delete_from_disk(self, purge_cdn=True)` (`app/models.py:421`), so the
   community path's bare call passes exactly what the site path passes
   explicitly and the two are identical. Caught in pre-flight against source and
   corrected in the spec itself (`90742a46`) before any task ran. The finding
   survives in a better form and is registered as D209: the asymmetric spelling
   invites a reader to infer a distinction that does not exist, **which is
   precisely what the spec's own author did**.
2. **The plan prescribed the shared `redis_double` fixture for every
   `delete_post_or_comment` test, and said the function locks four keys. Both
   halves were wrong.** `redis_double` cannot serve a redis-py lock at all in
   this environment -- fakeredis with no `lupa` implements no Lua, so
   `Lock.acquire()` succeeds on plain `SET NX PX` and `Lock.release()` raises
   `unknown command 'evalsha'` on every `__exit__`; and the function takes
   **seven** locks, not four (`app/activitypub/util.py:2214`, `:2220`, `:2222`,
   `:2238`, `:2245`, `:2249`, `:2254`). Task 1's implementer hit the first,
   found the campaign's two existing lock-only doubles, built a third after the
   `tests/test_inbox_dispatch_votes.py:145-158` pattern and **reported it rather
   than silently diverging**; the controller then verified the lock count and
   corrected the plan. Registered as D212, and the third copy of the double is
   now noted in `tests/README.md`'s existing fakeredis item.
3. **The spec assumed every conjunct of every guard in the slice was killable;
   one was not.** `site_ban_remove_data`'s reply query filters
   `user_id=blocked.id, deleted=False` (`:2310`), and dropping `deleted=False`
   killed nothing, because no fixture in the file seeded an already-deleted
   `PostReply`. This is the **third** instance in the campaign of the
   combinatorial gap that produced D186 and D197 -- and the second where the
   same clause appears at two call sites in one function with only one of them
   reachable. Registered as D211 and closed inside the same sub-project by
   Task 6's fix round. Its sibling in `community_ban_remove_data` did **not**
   repeat it: that function's `community_id` clause killed on the first attempt,
   because the fixture was built with two communities prospectively.

A fourth prediction came close enough to record: the spec expected 214 uncovered
statements to close and **200** did (missing statements 1415 -> 1215). The same
shape as sub-project 11's 87-against-89 -- not a shortfall, but the four
`tests/test_inbox_dispatch_*.py` suites already reached some of those statements
through the dispatcher, so covering the six functions properly could not claim
credit for all 214. The spec's headline prediction -- "toward **56%**" -- landed
at 55.1080%.

### 1. The most consequential finding in this slice, registered not fixed -- D200

Registered first and on its own, not buried in the list below, because it is the
only finding here that **silently corrupts stored data on an ordinary moderator
action**, and because it is the only one that required a round-trip test to
establish rather than a reading of source.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D200 | `delete_post_or_comment`'s `PostReply` branch (`app/activitypub/util.py:2237-2256`) vs. `restore_post_or_comment`'s (`:2289-2298`) | **Not fixed -- a delete-then-restore cycle leaves `community.post_reply_count` and `post.reply_count_cross_posted` one lower than it found them, and every further cycle loses one more.** Delete decrements four counters for a reply: `to_delete.author.post_reply_count` (`:2246`), `to_delete.post.reply_count` when the author is not a bot (`:2250`), `to_delete.post.reply_count_cross_posted` when it is truthy (`:2251-2252`) and `community.post_reply_count` (`:2255`). Restore increments **two**: `to_restore.post.reply_count` (`:2293`) and `to_restore.author.post_reply_count` (`:2294`). The words `post_reply_count` on a community and `reply_count_cross_posted` do not appear anywhere in `restore_post_or_comment`'s body. The `Post` branches are the control and they are symmetric -- delete decrements `community.post_count` (`:2221`) and `author.post_count` (`:2223`), restore increments both (`:2279-2280`) -- so this is specific to the reply halves, not a general complaint about counter hygiene. **The same asymmetry exists in the local, non-federated pair**, which is the sharper form of the finding: `app/shared/reply.py`'s `delete_reply` decrements all four (`:252-255`) and its `restore_reply` increments only two (`:280-281`), so the drift accrues from a moderator using the web UI as readily as from an inbound `Undo`. (`mod_remove_reply`/`mod_restore_reply` in the same file, `:400-450`, are symmetric with each other and touch neither of the two.) **The pin's own docstring originally said "nothing later notices or repairs it" and was corrected in the same wave as this entry** -- that was too strong, and the weaker claim is the supportable one. Nothing on the delete/restore path repairs either counter, but `community.post_reply_count` is recomputed from the `post_reply` table by `update_community_stats` (`app/shared/tasks/maintenance.py:283`, the recompute at `:313-315`), which the `daily-maintenance` and `daily-maintenance-celery` CLI commands run (`app/cli.py:838`, `:915`); and `post.reply_count_cross_posted` is recomputed for a whole cross-post set by the reply-creation path (`app/models.py:3094-3104`). So the drift is bounded by one maintenance cycle for the community counter, and unbounded for `reply_count_cross_posted` on a post that receives no further replies. Not fixed because correcting a counter changes numbers users already see, the historical drift is unmeasurable from the rows that survive, and the local pair would have to move in the same commit for the fix to mean anything -- all three are decisions outside this sub-project's authorisation. | not fixed, registered only | measured: `test_a_delete_then_restore_cycle_loses_two_counters` (`tests/test_ap_moderation.py`) seeds all four counters to 5, calls `delete_post_or_comment` and then `restore_post_or_comment` on the same reply in one test, and asserts `post.reply_count == 5` and `author.post_reply_count == 5` against `community.post_reply_count == 4` and `post.reply_count_cross_posted == 4`. `reply_count_cross_posted` is seeded to 5 deliberately: its decrement sits behind `if to_delete.post.reply_count_cross_posted:` (`:2251`), which is false at the column's `default=0`, so an unseeded test would not reach the line at all. `test_a_post_delete_then_restore_cycle_is_lossless` is the lossless `Post`-side control in the same file. The `app/shared/reply.py` half is reading-level: read directly at this commit, not covered by this sub-project's tests |

### 2. One defect fixed, test-first with a mutation-proved test -- D201

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D201 | `site_ban_remove_data` (`app/activitypub/util.py:2309-2346`, the line now at `:2320`) | **FIXED, commit `25de721d`. The function wrote `blocked.reply_count = 0` to a column `User` does not have, so site-banning a user never zeroed their reply counter.** `User` (class at `app/models.py:973`) declares `post_count` (`:1010`) and `post_reply_count` (`:1011`) and no `reply_count`; `reply_count` belongs to `Post` (`:1722`). SQLAlchemy accepted the assignment as **an ordinary Python attribute on the instance** -- it was never mapped, so it never reached the database and never raised. That is the entire explanation for how the defect survived: it sat one line below `blocked.post_count = 0` (`:2330`), which works because that column does exist, and differed from a working sibling only by a column name. A defect that crashes is found on its first execution; this one had to be **read** to be found. The sister function `community_ban_remove_data` decrements the real `blocked.post_reply_count` (`:2357`), which is what identified the correct target. The fix is one line: `blocked.reply_count` -> `blocked.post_reply_count`. | **fixed**, commit `25de721d` | measured: `test_a_site_ban_zeroes_the_users_reply_count` (`tests/test_ap_moderation.py`) seeds both `post_count` and `post_reply_count` to 5 and asserts both at 0 -- both, because `post_count` was always zeroed, so asserting `post_reply_count` alone would not distinguish the fix from a regression that zeroed some other attribute. The pre-fix failure was witnessed (`assert 5 == 0 ... post_reply_count`). Mutation-proved: restoring `blocked.reply_count = 0` kills exactly this test **by assertion, not by crash** -- the mutant sets a harmless Python attribute and commits cleanly. The reviewer established that this is a property of the model rather than of one run, by confirming `User` carries no `@validates` hook and no `__setattr__` override that could turn the plain-attribute write into a raise. `tests/test_inbox_dispatch_block.py` was checked for assertions encoding the old broken value and contains none, so no other suite was edited |

### 3. Nine items registered, not fixed -- D202-D210

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D202 | `restore_post_or_comment` (`app/activitypub/util.py:2268-2306`) vs. `delete_post_or_comment` (`:2214-2255`) | **Not fixed -- restore mutates the same counters delete does, under no lock at all.** `delete_post_or_comment` wraps every counter mutation in `with redis_client.lock(...)`: seven of them, three in the `Post` branch (`:2214` post, `:2220` community, `:2222` user) and four in the `PostReply` branch (`:2238` post_reply, `:2245` user, `:2249` post, `:2254` community). `restore_post_or_comment` contains no `redis_client.lock` call anywhere in its body, and does not even import `redis_client` -- `delete_post_or_comment` does so at `:2205`, inside its own body. So the same `community.post_count`, `author.post_count`, `post.reply_count` and `author.post_reply_count` fields are read-modify-written unserialised on the restore path and serialised on the delete path. **The scope is wider than the pair:** the controller verified that none of the other five functions in this slice takes a lock either -- the next `redis_client.lock` in the file is at `:2938`, in `notify_about_post_task`. So `site_ban_remove_data` and `community_ban_remove_data`, which decrement the same community and user counters in a loop, are unlocked too. Not fixed because adding locks to five functions is a concurrency change with its own deadlock-ordering questions, not a defect repair. | not fixed, registered only | reading-level, and verified independently by two agents against current source: the reviewer confirmed zero `redis_client.lock` occurrences in `:2268-2306`, the controller confirmed the seven in `:2214-2254` and the next occurrence at `:2938`. Not asserted by any test -- the suite's `redis_lock_only_double` replaces the lock with `contextlib.nullcontext()`, so no test in this file can observe locking either way |
| D203 | `restore_post_or_comment`'s cross-post guard (`app/activitypub/util.py:2281`) vs. `delete_post_or_comment`'s (`:2218`) | **Not fixed -- the restore guard drops one of delete's two conjuncts.** Delete tests `if to_delete.url and to_delete.cross_posts is not None:` before calling `calculate_cross_posts(delete_only=True)` (`:2218-2219`); restore tests only `if to_restore.url:` before calling `calculate_cross_posts()` (`:2281-2282`). The same two-conjunct spelling appears in both ban-removal functions (`:2328` and `:2368`), so restore is the **single** call site of the four that omits `cross_posts is not None`. Whether that matters depends on `calculate_cross_posts`'s own tolerance of a null `cross_posts`, which this sub-project did not establish and does not claim -- what is registered is the inconsistency itself, on a guard whose other three spellings agree. Not fixed for the reason the whole slice is registered rather than repaired: changing which posts get cross-post recalculation changes what peers see. | not fixed, registered only | reading-level: all four call sites read directly at this commit. Noted in `restore_post_or_comment`'s test docstrings; no test drives the differing branch, because doing so would require a post with a `url` and a null `cross_posts`, which this suite's factories do not produce |
| D204 | `unban_user`'s instance branch (`app/activitypub/util.py:2515-2539`) vs. its community branch (`:2540-2569`) | **Not fixed -- an instance-wide unban is recorded nowhere, while the ban that it reverses is.** The community branch ends with `add_to_modlog('unban_user', ...)` (`:2568-2569`), unconditionally and outside the `if blocked.is_local():` block, so a community unban is logged for local and remote users alike. The instance branch has **no `add_to_modlog` call anywhere**: it deletes the `InstanceBan` row (`:2518`), commits, notifies a local user and clears six memoized caches, and returns. Both branches of `ban_user` log (`:2458-2459` and `:2506-2507`), so this is the one cell of a four-cell table that is empty. The consequence is that a moderator reading the modlog sees an instance ban with no reversal beside it, and the ban looks live when it is not. Not fixed because writing a modlog entry is a new user-visible record on a federated code path, not a repair. | not fixed, registered only | measured: `test_an_instance_unban_writes_no_modlog_entry` (`tests/test_ap_moderation.py`) pins the current behaviour and cannot pass through a failed call -- it asserts `InstanceBan` count 0 **and** `ModLog` count 0, so an unban that did nothing at all would fail the first assertion. The reviewer confirmed the absence by reading `unban_user`'s whole body rather than trusting the report |
| D205 | `ban_user`'s existing-row guards (`app/activitypub/util.py:2424` and `:2462`) | **Not fixed -- the same guard, written twice in one function, has two different scopes, so re-banning behaves differently instance-wide and in a community.** In the community branch, `if not existing:` (`:2462`) wraps the **entire** remaining body through `add_to_modlog` (`:2506-2507`), so re-banning an already-banned user is a total no-op: no notification, no membership flag, no modlog entry. In the instance branch, `if not existing_ban:` (`:2424`) wraps **only** the `InstanceBan` insert and its commit (`:2425-2428`); the notification block, the six cache invalidations and `add_to_modlog` (`:2458-2459`) all sit outside it, so re-banning instance-wide re-notifies the user and writes a **second** modlog entry for a ban that was already in force. Same intent, two scopes, one function, and nothing in the code says which is meant. Not fixed because either direction is a behaviour change on a federated path -- and a peer that retransmits a `Block` is the common case, so the choice has real consequences. | not fixed, registered only | measured, as a discriminating pair: `test_re_banning_in_a_community_writes_no_second_modlog_entry` and `test_re_banning_instance_wide_still_writes_a_second_modlog_entry` (`tests/test_ap_moderation.py`) pin the two behaviours against each other. Both were confirmed load-bearing as a pair and neither vacuous alone. Line numbers verified against source by the Task 8 reviewer |
| D206 | `site_ban_remove_data` (`app/activitypub/util.py:2311-2319`) and `community_ban_remove_data` (`:2351-2360`) vs. `delete_post_or_comment` (`:2251-2252`) | **Not fixed -- both ban-removal loops soft-delete replies without decrementing `post.reply_count_cross_posted`, which the ordinary reply-deletion path does decrement on the same rows.** Each loop performs the same three maintenance steps as `delete_post_or_comment`'s `PostReply` branch -- `reply.post.reply_count -= 1` behind a bot check (`:2314-2315` / `:2354-2355`), `reply.community.post_reply_count -= 1` (`:2316` / `:2356`) and the ancestors' `child_count` update (`:2317-2319` / `:2358-2360`) -- and neither touches `reply_count_cross_posted`. Of the four places in `app/` that maintain that column, two decrement it on a reply deletion (`app/activitypub/util.py:2252` and `app/shared/reply.py:253`) and these two do not. **This is a third site of D200's counter**, drifting in the opposite direction: D200 loses one on a restore that never gave it back, this one keeps one that the underlying reply no longer justifies. Not fixed for D200's reasons, and because a ban removal deletes an unbounded number of replies at once, so the correction is a loop-body change whose blast radius is a whole user's history. | not fixed, registered only | reading-level: all four maintenance sites for `reply_count_cross_posted` enumerated by grep at this commit (`app/models.py:1723` declaration, `:3094-3104` recompute; `app/activitypub/util.py:2251-2252`; `app/shared/reply.py:253`) and both ban-removal loops read in full. No test asserts it -- the sub-project's ban-removal tests seed `post_reply_count` and `reply_count`, not `reply_count_cross_posted` |
| D207 | `site_ban_remove_data` (`app/activitypub/util.py:2310`, `:2323`, `:2335`) vs. `community_ban_remove_data` (`:2350`, `:2363`, `:2374`) | **Not fixed -- two functions written to the same shape query in two different SQLAlchemy styles, and one of the styles has already masked a defect in this codebase.** The site path uses `db.session.query(PostReply)`, `db.session.query(Post)` and `db.session.query(File).join(Post)`; the community path uses the legacy `PostReply.query`, `Post.query` and `File.query.join(Post)` for the identical three queries. The difference is not cosmetic in the way D209's is: legacy `Query.all()` **deduplicates entities automatically**, which is exactly what hid the cartesian-product join registered as D171 in `feed_outbox`/`feed_following` until this campaign read the SQL. The community path's `File.query.join(Post)` (`:2374`) is the same join shape on the legacy side. Not fixed because migrating query styles is the unrelated refactor D171 is already waiting on, and doing half of it here would leave the file more mixed, not less. | not fixed, registered only | reading-level: all six query sites read at this commit. Cross-reference D171 and `tests/README.md` fact 45, which states the deduplication behaviour and the class of bug it hides |
| D208 | The authorisation guard in `delete_post_or_comment` (`app/activitypub/util.py:2209-2212`) and `restore_post_or_comment` (`:2272-2275`) | **Not fixed -- the campaign's largest guard is copied, not shared.** Four disjuncts each -- author identity, same-instance admin, community moderator, community-instance admin -- with the same operators in the same order, **textually identical modulo two consistently substituted variable names** (`to_delete`/`deletor` -> `to_restore`/`restorer`). Verified as identical by the Task 4 implementer and independently re-verified against source by its reviewer rather than accepted from the report. The cost of the duplication is that an authorisation change has to be made twice, and this codebase's own history says that is where such changes go wrong: sub-project 11's D164 and sub-project 8's Task 1 both registered byte-identical dead copies, and this register's fact 49 exists because a correction landing in one of two copies is the campaign's most repeated mistake. Not fixed because extracting a shared predicate is a refactor of a security-relevant guard, which needs its own test-first change rather than a drive-by. | not fixed, registered only | measured, and re-measured by the final whole-sub-project review. **The property the entry rests on holds: no disjunct of the delete copy survives being dropped, and each of the four has a dedicated named test that isolates it** -- as do the second disjunct's two inner conjuncts, six mutations in all, every one of them killed. **What does not hold is the earlier claim that each was killed by exactly one test ("six sole kills across nine tests"). Four of the six are sole kills; two are multi-kills**, re-run against the file's 36 tests: dropping disjunct 1 (`to_delete.user_id == deletor.id`) gives **2 failed, 34 passed** -- `test_the_author_may_delete_their_own_post` (the dedicated isolation test) and `test_an_author_deleting_their_own_post_writes_no_modlog_entry`; dropping disjunct 3 (`community.is_moderator(deletor)`) gives **9 failed, 27 passed**; disjunct 2, disjunct 4 and disjunct 2's two inner conjuncts are sole kills. The cause is instructive rather than a fault in the tests: Task 1 wrote happy-path tests whose fixtures already isolated disjuncts 1 and 3 -- an author deleting their own post, a moderator deleting another user's -- and Task 2 then added dedicated isolation tests for those same two disjuncts. The dedicated tests are correct and self-documenting; they are simply not the only thing that dies. Disjunct 3's nine include both round-trip tests (`test_a_delete_then_restore_cycle_loses_two_counters` and `test_a_post_delete_then_restore_cycle_is_lossless`), and the reason they fail is worth knowing: the mutant's delete half refuses the moderator and moves nothing, but `restore_post_or_comment`'s **unmutated** copy of the same guard still admits them, so the restore half runs and increments counters the delete half never decremented -- the copy D208 is about is what makes the round trips sensitive to a delete-side mutation. Establishing the four-way isolation required moving the community off `instance_id=1`, because `User.is_instance_admin()` filters on the user's instance and `Community.is_instance_admin(user)` on the community's, and `make_community` hardcodes `instance_id=1` (now `tests/README.md` fact 61). The **restore** copy is covered by a refusal test and a permitted-path test, not by four separate disjunct kills |
| D209 | The `delete_from_disk` call sites in `site_ban_remove_data` (`app/activitypub/util.py:2337`) and `community_ban_remove_data` (`:2376`) | **Not fixed -- and, unusually for this register, not a behavioural defect either: it is an asymmetry of spelling that reads as one of behaviour.** The site path calls `file.delete_from_disk(purge_cdn=True)`; the community path calls `file.delete_from_disk()`. `File.delete_from_disk`'s signature is `def delete_from_disk(self, purge_cdn=True)` (`app/models.py:421`), so the bare call passes exactly what the explicit one passes and the two paths behave identically. It is registered because **this sub-project's own spec drew the wrong conclusion from these two lines** before the signature was checked, asserting a CDN-purge difference that does not exist; the claim was falsified in pre-flight and corrected in the spec at `90742a46`. Two call sites of one defaulted parameter, one naming the default and one not, is a standing invitation to that error. Not fixed because the repair -- spelling both the same way -- is a cosmetic change to production code, which this sub-project was not authorised to make. | not fixed, registered only | measured: `test_a_community_ban_purges_the_cdn_despite_its_bare_call` (`tests/test_ap_moderation.py`) doubles `File.delete_from_disk` at its binding site and asserts the flag's **real value** -- `calls[0][1] is True` -- so the suite states the non-difference rather than leaving the next reader to check the signature. **The test was originally named `test_a_community_ban_deletes_files_without_purging_the_cdn`, which read against what it asserts** -- the name carried the spec's falsified claim while the body and docstring carried the truth. Renamed by the controller in the same fix wave as this entry, and the docstring now records the old name so a reader meeting it in git history can place it. `site_ban_remove_data`'s twin (`test_a_site_ban_deletes_attached_files_and_purges_the_cdn`) asserts the same value on the explicit side |
| D210 | `Post.post_reply_count_recalculate` (`app/models.py:2702-2705`) | **Not fixed -- a second, unfired instance of the exact defect D201 fixed, in dead code.** The method assigns `self.post_reply_count = <SELECT COUNT(*) ... WHERE post_id = :post_id AND deleted is false>`. `Post` (class at `app/models.py:1700`) declares `reply_count` (`:1722`), not `post_reply_count` -- `post_reply_count` belongs to `Community` (`:571`) and `User` (`:1011`). So the method would set a plain Python attribute that is never persisted and never raises, exactly as `site_ban_remove_data` did, and the count it computes would be discarded. It has never been observed to do so because **nothing calls it**: `grep -rn 'post_reply_count_recalculate' app/` finds only its own definition. Registered rather than fixed for two reasons -- it is outside this sub-project's slice, and the right repair is ambiguous between renaming the target to `reply_count` and deleting an uncalled method. Its value here is corroborative: it shows D201 was not a one-off typo but a class of defect this codebase produces, which is why `tests/README.md` fact 57 states the general rule rather than the instance. | not fixed, registered only | reading-level: the method, both class boundaries and the column declarations read directly at this commit, plus the whole-tree grep for callers. Not covered by any test, and not coverable -- an uncalled method has no reachable branch |

### 4. Two test-suite findings, not production defects -- D211-D212

Recorded so nobody re-files either as a production defect, and because both are
harness failure modes that reading the production code cannot reveal.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D211 | `site_ban_remove_data`'s reply query (`app/activitypub/util.py:2310`) -- a **test-suite** finding | **Not a production defect. The `deleted=False` clause of `db.session.query(PostReply).filter_by(user_id=blocked.id, deleted=False)` was enforced by no test until this sub-project's Task 6 fix round, and the cause was a COMBINATORIAL GAP -- the third instance in this campaign, after D186 and D197.** Dropping the clause left all 24 tests then in the file green: every fixture seeded only undeleted replies, so the set the filter exists to exclude was empty and the mutant could not be distinguished. The **post-side** `deleted=False` at `:2323` was killed cleanly by an existing test, which is what makes this a per-call-site gap rather than a missing test for the function: one clause, two call sites, one of them unreachable by the fixtures at hand. The consequence had the clause ever been dropped is concrete rather than theoretical -- `site_ban_remove_data` would re-process replies it had already soft-deleted, decrementing `post.reply_count`, `community.post_reply_count` and the ancestors' `child_count` a second time for rows already accounted for, so counters would drift down on every re-ban. Closed in the same sub-project under a controller fix-round authorisation. | closed by a test in the same sub-project | measured: `test_a_site_ban_skips_replies_already_deleted` (`tests/test_ap_moderation.py`) seeds an already-deleted `PostReply` as the user's only content, so a working filter leaves every counter alone. After it, **both** `deleted=False` mutations kill exactly one distinct test each, sole and non-overlapping -- the post-side test seeds no reply at all and the reply-side test seeds only an undeleted post, so each kill is attributable to one call site. `git diff app/activitypub/util.py` verified empty after each mutation |
| D212 | `redis_double` (`tests/conftest.py:428-445`, the CAVEAT paragraph) against `delete_post_or_comment` (`app/activitypub/util.py:2214-2254`) -- a **test-suite** finding | **Not a production defect, and recorded because a plan told nine tasks to use a fixture that cannot work.** The shared `redis_double` fixture cannot serve a redis-py lock in this environment: fakeredis with no `lupa` implements no Lua, so `Lock.acquire()` succeeds on a plain `SET NX PX` and `Lock.release()` raises `unknown command 'evalsha'` on every `__exit__`. The failure mode is the awkward one -- the fixture looks correct, the lock is acquired, and the test dies on the way out of the `with` block. This is the **third** file in the repo to need a local lock-only double (`tests/test_inbox_dispatch_undo_content.py:35`, `tests/test_inbox_dispatch_votes.py:145-158`, and now `tests/test_ap_moderation.py`), and the pattern is identical in all three: patch `app.redis_client` -- the binding `delete_post_or_comment` reaches through its in-body re-executed import at `:2205` -- with an object whose `.lock(...)` returns `contextlib.nullcontext()`. The plan also stated the wrong lock count (four; there are seven). Registered rather than fixed because the alternative -- adding Lua support to the shared conftest fixture -- changes every suite that uses it and was outside this sub-project's authorisation. | not fixed, registered only; a local double is the accepted remedy | measured: the failure was hit by Task 1's implementer as real test failures, not reasoned about; the controller verified the documented cause in the fixture's own docstring and the two precedents, and the reviewer confirmed the new double patches the right binding and returns a genuine no-op context manager with no silent fallthrough to the real shared compose Redis. The seven lock sites were counted against source, and the next `redis_client.lock` in the file placed at `:2938`, outside the slice |

### 5. Three asymmetries deliberately NOT counted as defects

Recorded so nobody re-files them. Each was examined against source in this
sub-project and each has an explanation that survives reading.

- **`site_ban_remove_data` deletes the user's avatar and cover
  (`app/activitypub/util.py:2339-2344`) and `community_ban_remove_data` does
  not.** Defensible on its face -- a community ban should not destroy a user's
  profile images -- and the source says so in its own comments at `:2333-2334`
  and `:2373`. Pinned by `test_a_site_ban_deletes_the_users_avatar_and_cover`.
- **`ban_user` and `unban_user` clear six memoized caches on their instance
  branches (`:2451-2456`, `:2534-2539`) and four on their community branches
  (`:2501-2504`, `:2563-2566`).** The two extra are `banned_instances` and
  `blocked_or_banned_instances`, both instance-scoped, so the asymmetry is the
  correct one and the count difference is not a finding.
- **`ban_user` reads the reason from `core_activity['summary']` while
  `unban_user` reads it from `core_activity['object']['summary']`
  (`:2415-2416` vs. `:2511-2512`).** Consistent with an `Undo` wrapping the
  original activity, and almost certainly correct. Recorded because the nesting
  difference is a real trap for a test fixture: a top-level `summary` passes
  through `unban_user` proving nothing, which is why all three of this
  sub-project's `unban_user` tests nest one level deeper.

**Seven shapes worth carrying forward from this sub-project's rulings, now in
`tests/README.md` as facts 56-62:** a logging call that writes nothing unless a
config flag is set makes every assertion about its rows vacuous by default;
assigning an undeclared attribute to a SQLAlchemy instance is silent in both
directions, so a counter name must be grepped against the model before it is
trusted; `expire_on_commit` is default-`True` here, so a `db.session.refresh()`
added to observe a raw-SQL update is usually a no-op and a docstring calling it
load-bearing is a claim about the session config rather than the SQL; a
do/undo pair needs a **round-trip** test, because asserting that the undo leaves
a counter alone proves nothing unless the do moved it; a filter whose excluded
set is empty under the fixture is unkillable no matter how many tests exercise
the function; `make_community` hardcodes `instance_id=1`, so a fixture meant to
satisfy one clause of a guard can silently satisfy a second; and
`Community.has_poster(user)` falls back to counting replies, so "has posted
there" includes "has only replied there".

## Sub-project 13: the three mirrored actor-refresh tasks

`docs/superpowers/specs/2026-09-03-coverage-refresh-profiles-13-design.md` and
`docs/superpowers/plans/2026-09-03-coverage-refresh-profiles-13.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-03-coverage-refresh-profiles-13/`, not committed), on
branch `blentz`. Eleven tasks brought three Celery tasks in
`app/activitypub/util.py` under test -- `refresh_user_profile_task`
(`:654-773`), `refresh_community_profile_task` (`:783-1010`) and
`refresh_feed_profile_task` (`:1020-1179`) -- and fixed **five** defects in five
commits (`1ba9f0a2`, `84be0559`, `4cd0042c`, `ce849451`, `5113324a`) under this
sub-project's own bounded, explicit authorisation; this task (11) closes the
sub-project out with the findings register, the test-harness log, and the
coverage floor. `app/activitypub/util.py` measures **61.8397% blended**
(1847/2872 statements, 916/1596 branches; 1025 statements and 192 branch arcs
still missing) after this sub-project, up from 55.1080%. Full suite after this
sub-project: **3423 passed, 3 skipped, 6 subtests passed**, in 212.23s. Tests
live in `tests/test_ap_refresh_profiles.py` (58 test functions, none
parametrized). The coverage figure above is the controller's single
authoritative module-level measurement, taken after Task 10's last fix round on
a freshly torn-down stack; **no per-function residual breakdown was re-measured
at Task 11**, so this section claims the module figure and not "zero uncovered
statements" for any individual function.

**A FINAL FIX WAVE FOLLOWED (commits `d43ccaad` and the register commit after
it), after a whole-branch review of everything above.** It found two tests whose
mutants survived -- one of them with no `assert` statement at all -- and seven
docstrings making false claims about production structure; it added D232-D235,
four classes the nineteen entries above do not cover. **Read subsection 6 before
trusting any prose sentence in this section**, and note in particular that D235
now supplies the per-function residual the paragraph above declines to claim:
**the spec's first success criterion was not met**, 124 statements across the
three functions are uncovered, and the corrections to D216, D219, D222 and D225
are marked inline in their own rows.

**The spec's headline prediction was not met, and the near-miss is a trap worth
naming.** The spec said the blended figure would move from 55.1080% "toward
**64%**". It reached **61.8397%**. The number 64 does appear in the final
measurement -- as the *statement* figure, 64.3106% -- and that is a coincidence
of two different metrics, not the prediction landing. Anyone reading the two
documents side by side will see "64" in both and should not conclude the target
was hit: **it was missed by about 2.2 blended points**, and the reason is the
ordinary one from sub-projects 11 and 12 -- the spec's 310-uncovered-statement
budget assumed every statement in the three functions was uncovered and reachable
by this file alone, and some of it is neither.

Like 5c through 12 before it, this sub-project carried a **narrow, explicitly
authorised exception** to the campaign's report-don't-fix rule. Unusually, it
was exercised five times rather than once, and **the exception widened twice
during the slice on rulings recorded in the ledger**: the spec authorised two
crash fixes, Task 7's tests provoked a third crash of the same class that the
spec's up-front reading had missed (Fix C), the Task 10 reviewer found a fourth
(Fix D), and the controller found a fifth while reading Fix D's diff (Fix E).
`git diff --stat app/` is non-empty for exactly those five commits, totalling
under 60 production lines; every other task's `git diff --stat app/` is empty.

**Where fixing stopped, and the rule that stopped it.** Fix E was not the last
defect of its class -- two whole *families* of the same shape remain, catalogued
below as D218 and D219 with line numbers verified against source. The line the
controller drew, and it is a rule this campaign can reuse: **a defect is fixed
under a bounded authorisation when the correct spelling already exists in the
file and the fix is mechanical; it is registered when the fix would require
choosing new behaviour for a case the codebase has never handled.** Fixes A
through E all met the first test -- each copied a sibling guard nine to fifty
lines away in the same function. D218 does not: deciding what a refresh should
*do* when a peer omits `preferredUsername` or `publicKey` -- skip the actor,
apply it partially, count an instance failure -- is a design decision, not a
guard. `activity_json['name']` is the borderline case, since its own guarded
sibling sits on the very next line, and it goes with its family rather than being
fixed alone. The countervailing weight was stated too: five fixes had landed, the
slice still owed a coverage run and a final review, and a sixth widening risked
the slice never closing. **Cost if wrong: two families of remotely-triggerable
`KeyError`s stay in the tree one slice longer, fully documented, against a slice
that never lands.**

The slice exists because these three functions are one function written three
times, and -- as in sub-projects 8 through 12 -- the defects live in what the
copies do **differently**. That is not a rhetorical framing: **every one of the
five fixes was justified by a majority vote among siblings**, and in four of the
five the majority was two-of-three or four-of-five with the correct spelling
already present. The one place the majority pointed the other way is registered
rather than fixed (D219's three-against-one image branch), because there the
minority spelling is the *user* task's and correcting it would change what a
Mastodon-shaped `image` key does.

**Three of the plan's and briefs' claims were falsified, and each falsification
is worth more than the claim was.**

1. **A brief predicted Fixes A and B would shift `util.py:669` and `:674`**, the
   two bare-`except:` lines that three test docstrings hardcoded, and told Task
   10 to audit them by name. Wrong: all five fixes landed *below* `:674`, so the
   numbers never moved. The implementer converted those citations to content
   anyway, which is the right outcome from a wrong prediction -- see the
   register's own standing rule that line numbers here are dated records, not
   maintained pointers.
2. **A brief named the wrong column for the followers count.** It guessed
   `subscriptions_count`; the code writes `community.total_subscriptions_count`
   (`:985`), and `subscriptions_count` is never touched on this path. The plan's
   own self-review had flagged that name as a guess to be replaced rather than
   shipped, and Task 9's implementer read the source instead of transcribing.
3. **A fix round's brief named three call sites for Fix E; there are four.**
   `refresh_feed_profile_task`'s owners guard (`:1121`) is character-for-character
   the community moderators guard (`:947`) with one identifier changed, and
   carries the identical defect. Fixing the three the brief named would have
   produced exactly the one-clause-several-sites gap this campaign keeps hitting
   -- and the controller's own brief would have caused it. The majority signal
   for Fix E is therefore one-of-**four** right, not one of three.

A fourth item is bookkeeping rather than a defect, but it is the **fifth** count
slip in this campaign and the cause was identical every time: a task legitimately
grows after its expected-test count is written, and every later count stays
internally consistent while being wrong by the same constant. It happened twice
here (Task 4's implementer added seven tests beyond its brief's four; Task 7's
fix round added a pin). The remedy the fourth slip identified was applied at the
fifth: correct the plan *before* generating the next brief, because generation is
what freezes a stale number.

### 1. Five defects fixed, test-first with mutation-proved tests -- D213-D217

Each was pinned by a test asserting the *crash* first, the pin then inverted to
assert the fixed behaviour, and each fix mutation-proved at every call site
separately. All five are **remotely triggerable by any peer being refreshed
from**, which is why they were fixed rather than registered.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D213 | `refresh_community_profile_task` (`app/activitypub/util.py:789`) and `refresh_feed_profile_task` (`:1026`), against `refresh_user_profile_task` (`:660`) | **FIXED, commit `1ba9f0a2`. Refreshing a community or a feed whose actor row had a NULL `instance_id` raised `AttributeError` out of the task.** Both `instance_id` columns are nullable -- `app/models.py:575` for `Community` and `app/models.py:4090` for `Feed`, neither declaring `nullable=False` -- and both `.instance` attributes resolve to `None` when the FK is NULL, so `None.online()` raises. **The two are wired differently, which is worth stating precisely rather than lumping together**: `Community.instance` exists only as the `backref='instance'` on `Instance.communities` (`app/models.py:116`), while `Feed.instance` is a direct `db.relationship` declared on `Feed` itself (`app/models.py:4091`), as `User.instance` is (`app/models.py:1061`). The null behaviour is identical; the declaration site is not. The user task's guard reads `if user and user.instance_id and user.instance.online():` and has always carried the `instance_id` conjunct; its two copies read `if community and community.instance.online():` and `if feed and feed.instance.online() and not feed.is_local():` and did not. **Two of three wrong, one right, with the right one 130 and 370 lines above the wrong ones in the same file** -- which is the whole justification for the fix: the correct spelling was already in the file and the change is mechanical. The fix inserts the conjunct in the position the user task has it. | **fixed**, commit `1ba9f0a2` | measured: `test_a_community_with_no_instance_is_skipped` and `test_a_feed_with_no_instance_is_skipped` (`tests/test_ap_refresh_profiles.py`), each originally written as a `pytest.raises(AttributeError, match=...)` pin narrowed to the verbatim message `'NoneType' object has no attribute 'online'` -- obtained by forcing a match mismatch so pytest printed the real text, not reconstructed from the `match=` fragment -- then inverted in place. The escape was traced by reading each function end to end rather than assumed: the only `except` blocks between the guard and the crash are `except httpx.HTTPError` around the *fetch* calls, and the sole catch-all is `except Exception: session.rollback(); raise` (`:1006-1008`, `:1175-1177`) which **re-raises**. Mutation: removing the conjunct at one site with the other intact gives `1 failed, 46 passed` naming that site's test, twice, so the two call sites are independently killed |
| D214 | `refresh_community_profile_task` (`:800-805`) and `refresh_feed_profile_task` (`:1036-1041`), against `refresh_user_profile_task` (`:677-683`) | **FIXED, commit `84be0559`. A peer serving a non-JSON body at its actor URL raised `json.JSONDecodeError` straight out of a community or feed refresh.** The user task wraps `activity_json = actor_data.json()` in `try: ... except JSONDecodeError: user.instance.failures += 1; session.commit(); return`; its two copies called `.json()` bare. Same two-of-three-wrong shape as D213 and the same remote trigger -- the peer chooses the response body. **The fix gains more than crash-safety: it gives the community and feed paths the instance failure-counting the user path already did**, so a peer that starts serving garbage now accrues failures on all three paths instead of one, which is what the instance-health machinery reads. | **fixed**, commit `84be0559` | measured: `test_a_malformed_community_document_counts_an_instance_failure` and `test_a_malformed_feed_document_counts_an_instance_failure`, pins narrowed to `json.JSONDecodeError` matching `Expecting value` and inverted. **Both assert `failures == 6` against a seeded 5**, deliberately, so an increment is distinguishable from an assignment -- the shape established by the user task's own control test, `test_a_malformed_actor_document_counts_an_instance_failure`. Mutation: removing one `try/except` with the other intact gives `1 failed, 46 passed` naming that site's test, twice, with a grep between mutations confirming exactly one of the two `instance.failures += 1` lines was present |
| D215 | `refresh_feed_profile_task`'s following-collection fetch (`:1155-1156`) | **FIXED, commit `4cd0042c`. The feed task fetched `feed.ap_following_url` with no check that it was set, so refreshing a feed whose peer had never sent a `following` key raised `httpx.HTTPError: HTTPError: invalid uri`.** The column has no default; `get_request` rejects the empty host `furl(None)` produces, at `app/utils.py:131-134` via `is_invalid_get_request_uri`. **Found by Task 7's tests rather than by the spec's up-front reading** -- the implementer transcribed a happy-path test literally and watched it crash -- which is what the tests are for, and is the reason this sub-project's fix count is five rather than the two the spec authorised. The controller then enumerated every collection fetch in the trio: `:941` `if community.ap_moderators_url:`, `:979` `if community.ap_followers_url:`, `:988` `if community.ap_featured_url:` and `:1115` `if feed.ap_moderators_url:` are all gated; this one alone was not. **Four of five gated, one not.** The fix re-indents the fetch, its status check and the feed-item loop under `if feed.ap_following_url:`, so the gate captures exactly the fetch and nothing above it. | **fixed**, commit `4cd0042c` | measured: `test_a_feed_with_no_following_url_is_skipped`, written first as `test_a_feed_with_no_following_url_crashes` against a **fresh** run of the null column rather than the earlier accidental failure -- the two could have differed -- then inverted. The controller also asked whether a *fourth* ungated `get_request` existed further down the feed task before authorising one fix of two; it does not, and the remaining fetches are inside `find_actor_or_create`, out of scope. Mutation: one call site, one kill |
| D216 | `refresh_feed_profile_task`'s following-collection fetch (`:1157-1163`), against its five sibling collection fetches | **FIXED, commit `ce849451`. Inside D215's new gate, `res = get_request(...)` then `res.json()` checked no status code and guarded no decode, so a peer returning 500 or a non-JSON body at its following collection raised straight out of the task.** Every sibling collection fetch in the trio checks status -- `:944`, `:981`, `:990`, `:1118` -- and so does the following-collection fetch elsewhere in the same file, in `actor_json_to_model` (`:1574`). **Five sibling fetches right, one wrong, in the function this slice owns.** Same class as D214 and the same remote trigger. **The decode guard deliberately does not copy D214's spelling**: none of the five siblings has a decode guard at all, so there was no sibling to copy, and the fix matches the `try/except JSONDecodeError: object_request.close(); return` in **`verify_object_from_source`** (`:4499-4503`, the function spanning `:4458-4551`) instead. The stated reason, which the controller accepted: by the time this fetch runs, the **same instance** has already served, decoded and applied a well-formed actor document, so counting an instance failure here would mark a demonstrably healthy instance as failing. | **fixed**, commit `ce849451` | measured: `test_a_non_200_following_response_creates_no_feed_items` and `test_a_malformed_following_collection_creates_no_feed_items`. The choice not to count a failure is **pinned rather than left as an unexercised preference**: `failures` is seeded to 5 and asserted still 5. **The non-200 pin's body is deliberately valid JSON naming a resolvable community** -- garbage at 502 would have made both mutations die by `JSONDecodeError` and rendered the two guards indistinguishable. As written, dropping the status check creates a `FeedItem` and flips a count while dropping the decode guard raises, so two guards give two different kills, each leaving the other test passing. The same round also closed the statement-coverage gap in the following loop's body, which had never run with a non-empty collection (`test_a_following_collection_entry_becomes_a_feed_item`, `test_a_following_entry_that_resolves_to_nothing_is_skipped`). **CORRECTED at commit `d43ccaad`: this sentence said "the last statement-coverage gap", and the test docstring it came from said "THE LAST STATEMENT-COVERAGE GAP IN THIS SUB-PROJECT" in capitals.** Both were false when written. **124 statements across the three tasks remained uncovered** -- see D235 for the per-function breakdown |
| D217 | Four collection guards: `refresh_community_profile_task`'s moderators (`:947`) and followers (`:984`), `refresh_feed_profile_task`'s owners (`:1121`) and its following loop (`:1166-1167`) -- against the featured guard (`:993`) | **FIXED, commit `5113324a`. Four guards subscripted a peer-controlled JSON document without first checking the key was there, so a peer returning an object with no `type` key -- or, for the following loop, no `items` key -- raised `KeyError` out of the task.** The featured guard is the correct spelling and reads `if featured_data and 'type' in featured_data and featured_data['type'] == 'OrderedCollection' and 'orderedItems' in featured_data:`. Its three near-neighbours dropped the `'type' in` membership check, and the following loop subscripted `following_collection['items']` with no check at all. **One of four right, and the right one sits nine lines below two of the wrong ones in the same function** -- the same majority signal that justified D213, D214 and D215. Treated as one defect at four sites, one commit, four separate mutation kills, matching how D213 and D214 were treated. **The brief named three sites; the implementer found the fourth** (the feed owners guard, character-identical to the community moderators guard with one identifier changed) and reported it rather than shipping the gap. | **fixed**, commit `5113324a` (with the missing conjunct pin added in `979ef950`, see D229) | measured: `test_a_typeless_moderators_document_is_skipped`, `test_a_typeless_followers_document_is_skipped`, `test_a_typeless_owners_document_is_skipped` and `test_a_following_collection_with_no_items_key_is_skipped`, four distinct sole kills. Shielded-conjunct check done and **negative**: deleting each guard's leading truthiness conjunct still crashes on `'type' in None` with a `TypeError`, which is how the featured guard's own null test already kills it. Diff read by the controller: four sites each gaining `and 'type' in <data>` in the featured guard's position, plus `if following_collection and 'items' in ...` around the loop; minimal, uniform, no drive-by |

### 2. Two families registered, not fixed -- D218-D219

These are the spec for a follow-on slice. They are registered rather than fixed
under the rule stated at the head of this section, and both were catalogued with
line numbers **verified against source at this commit** by the implementer who
found them, precisely so the next slice does not have to re-derive them.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D218 | All three tasks' required-key reads: `refresh_user_profile_task` (`app/activitypub/util.py:697`, `:722`), `refresh_community_profile_task` (`:819`, `:832`), `refresh_feed_profile_task` (`:1054`, `:1057`) | **Not fixed -- the actor document's own "required" keys are read unguarded in all three tasks, so a peer omitting any of them raises `KeyError` out of the refresh.** Six sites, two keys: `activity_json['preferredUsername']` (`:697`), `activity_json['name']` (`:819`, `:1054`), and `activity_json['publicKey']['publicKeyPem']` (`:722`, `:832`, `:1057`). ActivityPub requires none of these of a `Person`, `Group` or `Feed`, and the trigger is entirely peer-controlled. **This is an oversight rather than a deliberate strictness**, and the file says so itself: `user.title` on the line *immediately after* `:697` is guarded by `if 'name' in activity_json:` (`:698`), so the same document is treated as untrustworthy one line later. Not fixed because the correct spelling does **not** already exist in the file: deciding what a refresh should do when a peer omits `preferredUsername` or `publicKey` -- skip the actor entirely, apply the rest of the document, count an instance failure -- is choosing new behaviour for a case this codebase has never handled, which is a design change and not a guard. `activity_json['name']` is the borderline member, since a guarded sibling *is* adjacent, and it is kept with its family rather than fixed alone. | not fixed, registered only | reading-level, verified against source at this commit by the Task 10 implementer and re-verified at Task 11. Not covered by any test: every document `_person_document`, `_group_document` and `_feed_document` produce carries all three keys, deliberately, because the slice's brief was the crash paths it was authorised to fix |
| D219 | List-entry subscripts, empty-list indexing and three mirrored-pair asymmetries across all three tasks | **Not fixed -- the same unguarded-subscript class as D217, in the *entries* of documents rather than their envelopes, plus three places where the three copies disagree with each other.** Five groups, all verified against source at this commit. **(a) Entry subscripts with no membership check:** `field_data['type']`, `field_data['value']`, `field_data['name']` (`:715-718`), `ap_language['identifier']` and `ap_language['name']` (`:889`), `item['id']` (`:998`), `actor['id']` (`:969`) -- each a `KeyError` on a peer-supplied list entry. **(b) `IndexError` on an empty list:** `activity_json['icon'][-1]` at `:733`, `:860` and `:1081`, and `activity_json['image'][0]` at `:755`, `:875` and `:1096` -- six sites where `isinstance(..., list)` is checked and emptiness is not, so `icon: []` raises before the `'url' in` test can run. **(c) The feed owners loop does not unwrap dict entries where the community moderators loop does:** `:1144-1145` reads `for actor in owners_data['orderedItems']: if actor.lower() ...` where `:967-969` first does `if isinstance(actor, dict): actor = actor['id']`. So an owners collection of objects -- the shape Lemmy sends and the shape the *sibling* loop was written to accept -- raises `AttributeError` on `dict.lower`. **(c) IS REGISTERED INCONSISTENTLY AND SHOULD BE LIFTED OUT FIRST. Added at commit `d43ccaad` on the whole-branch review's ruling:** (c) meets this sub-project's own stated rule for fixing rather than registering -- the correct spelling already exists in the same file, in the loop this one was copied from, and the change is two lines transcribed verbatim. That is the exact justification D213 and D214 used to fix rather than register. It is grouped here only because the other four members of D219 genuinely need a decision about what to do with a malformed entry, and (c) does not: the sibling already made that decision. **Not fixed in this fix wave** -- the wave's authorisation was "production code must not change" -- so the follow-on slice should take (c) out of D219 and fix it, ahead of the rest of the family. **(c) IS NOW FIXED, at commit `c9cc56fc` (sub-project 14, Task 11), which lifted it out of D219 exactly as this entry directed.** The two lines from the community moderators removal loop were transcribed into the feed owners removal loop, and the fix is pinned by `test_object_shaped_owners_entries_are_unwrapped_before_comparison` in `tests/test_ap_refresh_profiles.py`, which serves an owners collection whose `orderedItems` entries are `{'type': 'Person', 'id': ...}` objects and seeds a second owner absent from that collection, so the removal pass must remove the right row rather than merely not raise. Reverting the two production lines alone fails that pin and no other test in the file. **(a), (b), (d) and (e) remain not fixed and registered only** -- each still needs the decision about malformed entries that (c) did not. Note also that the two moderators loops in the community task disagree with **each other**, not just across tasks: the membership loop (`:948-950`) passes the raw entry to `find_actor_or_create`, which unwraps a dict itself (`app/activitypub/util.py:287-288`), while the removal loop (`:967-968`) unwraps inline -- so a dict entry survives the first loop by accident of the helper and the second by design, and the feed's owners removal loop survives neither. **(d) The user task's `image` branch omits the `'url' in` check that the community's (`:873`), the feed's (`:1094`) and the user's own `icon` branch (`:731`) all make:** `:746-753` tests `isinstance(activity_json['image'], dict)` and then reads `activity_json['image']['url']` three times (`:747`, `:749`, `:750`). **Three of four right, and here the minority spelling is the user task's** -- which is why this one is registered even though the majority is clear: adding the check changes what a Mastodon-shaped `image` key does on the user path, and that is behaviour, not a guard. **(e) Entry-shape asymmetry across the four collections**, recorded because it is what makes a single fix impossible: the following collection takes bare strings, moderators and owners take string-or-object, and featured requires `item['id']`. A uniform entry-validation helper would have to reconcile three different contracts. Not fixed for D218's reason -- every member needs a decision about what to do with the malformed entry, not just a guard. | **(c) fixed at `c9cc56fc`**; (a), (b), (d) and (e) not fixed, registered only | reading-level: every line above read directly at this commit. Not covered -- the suite's document helpers produce well-formed entries throughout, and the four `..._typeless_...` tests added by D217 pin the *envelope* guards, not the entries |

### 3. Nine items registered, not fixed -- D220-D228

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D220 | `refresh_user_profile_task`'s fetch handler (`app/activitypub/util.py:669` and `:674`), against `refresh_community_profile_task` (`:797`) and `refresh_feed_profile_task` (`:1033`) | **Not fixed -- the user task uses a bare `except:` where its two siblings use `except Exception:`, so it catches `KeyboardInterrupt` and `SystemExit` and a worker shutdown mid-refresh is swallowed into the signed-GET fallback.** Two sites, one nested inside the other: `:669` catches anything the `except httpx.HTTPError` above it missed and falls through to `signed_get_request`; `:674` catches anything *that* raises and returns silently. A Celery worker being terminated during an actor refresh therefore does not propagate -- it attempts a signed GET and then returns as though the refresh had completed normally. **The same code is also what made a production guard untestable through the fixture**, which is the connection worth registering rather than the two findings separately: Task 2 found the third conjunct of `:660` (`user.instance.online()`) unkillable because respx's `AllMockedAssertionError` is not an `httpx.HTTPError`, so it reached `:669`, the signed GET then failed too, and `:674` returned -- the guard firing and not firing produced the identical observable. **The defect and the obstacle to testing it are the same code.** Not fixed because narrowing an exception handler changes what a running worker does with a signal, which is a behaviour change on a federated path rather than a repair, and because the two siblings' `except Exception:` is not obviously the intended spelling either -- one of them (`:797`, `:1033`) is itself broader than the `except httpx.HTTPError` it shadows in the user task (`:667`). | not fixed, registered only | measured, indirectly and by two routes. `test_a_user_fetch_failing_outside_httpx_falls_back_to_a_signed_get` reaches `:669` by patching `ap_util.get_request` to raise `RuntimeError` -- not an `httpx.HTTPError`, so `except httpx.HTTPError` misses it and the bare `except:` catches it -- and records the `signed_get_request` call. Its two siblings, `test_a_community_fetch_failing_outside_httpx_propagates` and `test_a_feed_fetch_failing_outside_httpx_propagates`, assert the **opposite** outcome on the same stimulus, which is what pins the asymmetry as behaviour rather than as reading. That test must **not** request `http_mock`: `get_request` never runs, so `assert_all_called` would fail on any registered route. The unkillable-conjunct half was verified by the controller directly against `:661-675` |
| D221 | `refresh_user_profile_task` (`:669-675`) against `refresh_community_profile_task` (`:791-798`) and `refresh_feed_profile_task` (`:1027-1034`) | **Not fixed -- only the user task has a `signed_get_request` fallback, so a peer that requires HTTP signatures on actor fetches is refreshable for users and not for communities or feeds.** The user task, on any non-`httpx` error, loads `Site` id 1 and re-fetches with the instance actor's private key (`:670-673`); the community and feed tasks have no equivalent and simply `return` (`:798`) or propagate. On a signature-requiring peer the consequence is silent and permanent: a community's or feed's `ap_fetched_at` never advances, its title, description, icon, moderator list and follower count freeze at whatever was first ingested, and nothing surfaces the failure. Not fixed because adding an authenticated fetch to two federated code paths is a feature, not a guard -- it needs its own decision about whether a community refresh should present the instance actor at all. | not fixed, registered only | measured as a pinned three-way asymmetry by the same trio of tests named in D220's evidence: one asserts the fallback happens, two assert the identical stimulus propagates instead. Line ranges read directly at this commit |
| D222 | `refresh_feed_profile_task`'s guard (`:1026`) against `refresh_user_profile_task` (`:660`) and `refresh_community_profile_task` (`:789`) | **Not fixed -- only the feed task refuses to refresh a *local* actor.** Its guard ends `and not feed.is_local()`; the user and community guards have no such conjunct. `Feed.is_local()` (`app/models.py:4237-4238`) is `self.ap_id is None or self.profile_id().startswith(SERVER_URL)`. A local user or community reaching either sibling task would have its `ap_public_url` fetched over HTTP from this server's own address and its columns overwritten from the response. **The callers were enumerated before this entry was written, and they do not make the asymmetry harmless -- but they do explain the community half of it.** `schedule_actor_refresh` (`app/activitypub/actor.py:134-147`) tests `not actor.is_local()` at `:136` before dispatching any of the three, and it is the **only** caller of `refresh_user_profile`. It is not the only caller of `refresh_community_profile`: the inbox's `Group`/`Update` arm calls it at `app/activitypub/routes.py:1277`, and that call site **deliberately admits local communities** -- its guard is `if community.is_local() and not community.is_moderator(user):` (`app/activitypub/routes.py:1274`), so a *remote moderator of a local community* sending an `Update` is exactly the case it exists to serve. **Adding the feed's conjunct to the community task would break that path**, so the honest reading is that the community task's omission is correct and the finding narrows to the user task, whose sole caller already guards it and where the conjunct would therefore be redundant rather than load-bearing. Not fixed for that reason: what looked like two tasks missing a guard is one deliberate difference, one redundancy, and no established defect -- and the repair that would make the three copies agree is as likely to be *removing* the feed's conjunct as adding two. **Registered because the reading cost real effort and the next reader should not have to repeat it**, and because the shape -- one of three copies defending itself while the other two rely on their callers -- is where this campaign's history (D208, and this register's standing note on corrections landing in one of two copies) says things go wrong. | not fixed, registered only; the community half is explained rather than open | **CORRECTED at commit `d43ccaad`; this column previously claimed "measured on the feed side" and it was not.** `test_refreshing_a_local_feed_does_nothing` did seed a genuinely local feed via `make_local_feed` (which leaves `ap_id = None` and so satisfies `is_local()`'s first disjunct, `app/models.py:4237-4238`) -- but it carried **no `assert` statement at all**, and the conjunct survived its own deletion. `make_local_feed`'s host is `test.piefed.local`, and `is_invalid_get_request_uri` (`app/utils.py:5503-5504`) refuses any `.local` host, so `get_request` raises `httpx.HTTPError` from its own first line **before httpx is reached** and therefore before respx or `block_outbound_http` can observe anything. With the conjunct deleted the task took the `except httpx.HTTPError:` retry, slept, was refused again and returned quietly -- indistinguishable, under an outbound-HTTP observable, from the guard firing. The test now spies on `ap_util.get_request` (the pattern the three user-guard tests in the same file use for the same reason) and asserts both that no fetch was attempted and that `title`, `public_key` and `ap_fetched_at` are untouched. Mutation-proved at that commit: deleting `and not feed.is_local()` alone fails on `assert calls == []` with `[('https://test.piefed.local/f/localnews',)] == []`. **So the conjunct is now genuinely measured on the feed side.** No equivalent test exists for the other two, because there is no branch there to reach. **Every caller of all three tasks enumerated by grep at this commit**: `app/activitypub/actor.py:143`, `:145`, `:147` and `app/activitypub/routes.py:1277`, plus the three `current_app.debug` wrappers themselves |
| D223 | `refresh_community_profile_task`'s signature (`app/activitypub/util.py:784`) against `refresh_user_profile_task` (`:655`) and `refresh_feed_profile_task` (`:1021`) | **Not fixed -- only the community task accepts an `activity_json` parameter, so only it can be driven from a document the caller already holds.** `refresh_community_profile_task(community_id, activity_json)` skips the whole fetch-and-retry block when the caller passes a document (`:790`, `if not activity_json:`); the user and feed tasks take an id alone and always fetch. The consequence is a real efficiency and correctness asymmetry on the inbox path, and it has a live call site: the `Group`/`Update` arm passes the received document straight through (`app/activitypub/routes.py:1277`), so that community is refreshed with **zero** outbound requests, while the identical situation for a `Person` or a `Feed` costs an outbound fetch that may return a *different* document than the one just received. Not fixed because widening two task signatures changes their call contract across the codebase and, for the user task, would also have to decide what happens to the `signed_get_request` fallback that only exists to serve the fetch it would be skipping. | not fixed, registered only | measured: `test_refreshing_a_community_applies_a_supplied_document` registers **no** `http_mock` route at all and still applies the document, which under `assert_all_called=True` is a positive assertion that no fetch happened; `test_refreshing_a_community_applies_a_fetched_document` is its fetching control. The two sibling signatures read at this commit |
| D224 | All three tasks' retry sleeps (`:664`, `:794`, `:1030`) | **Not fixed -- all three tasks sleep `randint(3, 10)` seconds *inline in the Celery worker* rather than deferring the retry to the broker.** Every failed actor fetch therefore parks a worker process for up to ten seconds doing nothing, and the parking is proportional to how badly a peer is behaving: an instance that has gone away costs three to ten worker-seconds per actor refreshed against it, and these tasks are scheduled per actor by `schedule_actor_refresh`. Celery's own `self.retry(countdown=...)` releases the worker; `time.sleep` does not. Not fixed because converting to a broker retry changes the task's signature (it must be bound), its idempotency requirements and its failure accounting all at once, and because the same `time.sleep(randint(3, 10))` pattern appears elsewhere in this file -- fixing three sites would leave the file more mixed, not less. | not fixed, registered only | reading-level: all three sites read at this commit. The suite neutralises them with a `no_real_sleeping` fixture, which is itself the evidence that the sleep is real and inline -- `test_a_failed_fetch_is_retried_once` and its two siblings would otherwise cost up to thirty seconds |
| D225 | The membership loops in `refresh_community_profile_task` (`:948-949`) and `refresh_feed_profile_task` (`:1122-1123`) | **Not fixed -- `time.sleep(0.5)` runs once per collection entry, inline in the worker, inside both membership loops.** The sleep is the first statement of each loop body, before `find_actor_or_create`, so the cost is paid for every entry whether or not the actor turns out to be resolvable or already known. A community advertising two hundred moderators parks a worker for a hundred seconds on the sleeps alone. The peer chooses the length of the collection, so the cost is peer-controlled. Not fixed because the sleep is presumably a deliberate rate-limit against the peer being refreshed from, and replacing it needs a real rate-limiting decision -- per-host, per-task, or none -- rather than deletion. **Compounds with D226**, which is the other per-entry cost in the same loops. | not fixed, registered only | reading-level: both sites read at this commit, and both are inside the loop body rather than around it. **CORRECTED at commit `d43ccaad`: this column previously said the sleeps were "neutralised in the suite by the same `no_real_sleeping` fixture". They are not.** `tests/test_ap_refresh_profiles.py` declares no `pytestmark`, so the fixture is per-test, and the six tests that request it -- the four retry pins and the two non-httpx fallback/propagation pins -- are exactly the tests that reach the `time.sleep(randint(3, 10))` in the *fetch retry*, not these loops. The three tests that actually enter a membership loop (`test_the_moderators_url_is_taken_from_attributed_to`, `test_the_moderators_url_falls_back_to_the_kbin_spelling`, `test_a_feed_owners_url_is_fetched_and_recorded`) do **not** request it, and each serves a one-entry collection, so each pays one real 0.5-second sleep -- about 1.5 seconds of the file's runtime, and the reason the file's own wall-clock exceeds what its work justifies. No test asserts the sleep, and none should: it is the *unrequested* fixture that is the finding here, because a suite that appeared to neutralise the sleep would hide exactly the peer-controlled cost this entry is about |
| D226 | `find_actor_or_create` call sites inside the collection loops (`:950`, `:1124`, `:1169`), against its default at `:280` | **Not fixed -- `find_actor_or_create`'s `create_if_not_found` defaults to `True`, so every unresolvable entry in a peer-supplied collection triggers an inline outbound fetch: one per entry, and the peer chooses the entries.** The signature is `def find_actor_or_create(actor: str, create_if_not_found=True, community_only=False, feed_only=False, ...)` (`:280`); none of the three call sites in these tasks passes `create_if_not_found=False`. So a moderators, owners or following collection listing a hundred actor URLs this instance has never seen produces a hundred `create_actor_from_remote` fetches, in sequence, in the worker, on top of D225's fifty seconds of sleeping -- **a peer-controlled fan-out**, and the strongest of the three performance findings here because the peer controls both the count and the destinations. Not fixed because passing `create_if_not_found=False` would change what a refresh *does* -- moderators and owners that this instance has not met would silently stop being recorded -- which is exactly the design decision the register/fix line excludes. | not fixed, registered only | measured, from the side that had to work around it: the D217 pin for the following loop's False arm had to use the ActivityStreams Public URI rather than a merely-absent community URL, because an absent one reaches `create_actor_from_remote` and fetches. Signature and all three call sites read at this commit. Cross-reference `tests/README.md`'s fact on actor lookups fetching, which this finding produced |
| D227 | `refresh_user_profile_task` (`:654-773`) against `refresh_community_profile_task` (`:783-1010`) and `refresh_feed_profile_task` (`:1020-1179`) | **Not fixed, and registered as the structural shape of the whole slice: the user task fetches nothing at all beyond its actor document.** The community task fetches up to three further collections (moderators `:942`, followers `:980`, featured `:989`); the feed task fetches up to two (owners `:1116`, following `:1156`). The user task's work ends when the `Person` document is applied. That is why the user task has no D215 (no gate to omit), no D216 (no second fetch to check) and only one site in D217's four. It is registered rather than counted as a defect on either side, because the asymmetry may simply reflect that nothing in this codebase reads a remote user's own collections -- but it is the fact that explains the shape of every other entry here, and it means **the user task is not the template it otherwise looks like**: it is right about the guard (D213), right about the decode (D214) and right about the failure count, and it is simply never exercised on the code paths where its two copies go wrong. | not fixed, registered only | reading-level, confirmed by Task 9 while covering the opt-in collection fetches: every `get_request` in the three functions enumerated, five of them in the community and feed tasks and one in the user task |
| D228 | `refresh_feed_profile_task`'s following loop guard (`:1166`) against its three siblings (`:947`, `:984`, `:1121`, `:993`) | **Not fixed -- a deliberate, flagged asymmetry introduced by D217's own fix: the following loop's guard took the membership half and no type check, so it is now weaker than its three siblings.** It reads `if following_collection and 'items' in following_collection:` where the other four read `... and 'type' in <data> and <data>['type'] == '<Collection kind>' and '<items key>' in <data>`. Two reasons, both recorded rather than settled. The implementer's: every feed test in the suite serves `{'items': []}` with no `type` key and the loop has never made a type check, so adding one would break six tests and make the task refuse documents it accepts today -- inside a commit whose stated job is stopping a crash. The reviewer's, sought as a second opinion and better: in the three sibling guards `type` is **load-bearing for dispatch** -- `Collection` says read `totalItems`, `OrderedCollection` says read `orderedItems` -- whereas the following loop reads one key and then validates every entry individually through `find_actor_or_create` plus an `isinstance` check, so a hostile `{"type": "Person", "items": [...]}` buys an attacker nothing that `{"items": [...]}` does not. A type check there would add rejection, not safety. **The loop is now more permissive than its siblings, which is the safe direction for a crash fix.** Registered so the next reader meets the reasoning rather than the inconsistency. | not fixed, registered as a live decision | measured: `test_a_following_collection_with_no_items_key_is_skipped` and `test_a_null_following_collection_is_skipped` pin both halves of the guard as written. The four sibling guards read at this commit |

### 4. Three test-suite findings, not production defects -- D229-D231

Recorded so nobody re-files any as a production defect, and because all three
are mutation-testing failure modes that reading the production code cannot
reveal. **D229 is the most valuable thing this sub-project found and it is not
PyFedi-specific.**

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D229 | D217's new leading conjunct at `app/activitypub/util.py:1166` -- a **test-suite** finding | **Not a production defect. A fix that ADDS a conjunct must be mutation-proved by deleting THAT CONJUNCT ALONE; mutating the whole guard to `if True:` is a site-level proof, not a conjunct-level one -- and the gap is SELF-CONCEALING.** D217's mutation table recorded a green kill for the following-loop site, produced by replacing the whole guard with `if True:`. That answers "does anything depend on this *line*?" when the fix raises "does anything depend on this *clause*?" Deleting only `following_collection and` survived the entire file: **57 passed, zero failures**, because every feed test served a truthy dict and no test served JSON `null` at a following URL (`text='null'` appeared exactly once in the file, at the featured guard). A production behaviour a commit message advertised, with zero coverage. It is worse than a plain gap because **the site-level mutant does die**, so the table shows a kill and the missing proof leaves no trace -- nothing in the artefacts contradicts itself. Caught by the fix-round reviewer as a MUST-FIX, confirmed by re-running the narrower mutation before fixing, and closed by `test_a_null_following_collection_is_skipped` (`979ef950`). Round 2's mutation table was corrected in place with the error stated rather than quietly replaced. | closed by a test in the same sub-project | measured, both before and after: the conjunct-only mutation gave `57 passed, 0 failed` before the pin and a sole kill after. `git diff app/activitypub/util.py` verified empty after every mutation. Now `tests/README.md` fact 68 |
| D230 | D217's leading truthiness conjuncts at `:947`, `:984` and `:1121` -- a **test-suite** finding | **Not a production defect. D217's fix narrowed the discriminator set at its three sibling sites, so a later attempt to pin those conjuncts must serve JSON `null` and not `{}`.** Before the fix, deleting `mods_data and` (or its followers/owners twins) and serving an empty object raised `KeyError` on `{}['type']` and would have killed. After it, `'type' in {}` is merely `False` and the guard skips, so the empty-dict document no longer distinguishes the mutant -- **`null` is the only discriminator left.** Established by measurement, not by reasoning: deleting the truthiness conjunct at all three sites at once gives `58 passed, zero failures`, so none was killed *before* D217's fix either. **Nothing regressed** -- the conjuncts were already unpinned -- but the cheap way to pin them has been taken away, and the next slice to try `{}` and see it survive would have no way to tell this from a real gap. The general shape: **a defensive fix can silently shrink the set of inputs that kill a neighbouring mutant, without weakening any assertion and without failing anything.** | recorded, not closed -- the three conjuncts remain unpinned | measured: the three-at-once mutation run, and the pre-fix/post-fix behaviours of `{}` at each site derived from the guard text at this commit. Distinct from D229, which was a new site with a new conjunct; these are pre-existing sites whose kill inputs changed |
| D231 | The `session.rollback()` in all three tasks' outer handlers (`:770`, `:1007`, `:1176`) -- a **test-suite** finding | **Not a production defect, and recorded because the first explanation offered for it was too narrow.** The `session.rollback()` beside each `raise` is *covered* by the tests that reach the handler but is **not killable**: deleting it fails nothing. The Task 10 report argued that nothing uncommitted is pending at the only reachable raise point, which is true and narrower than the truth. The reviewer found the stronger reason: raise-after-dirty-write points **do** exist in all three functions, but `get_task_session()` (`app/utils.py:3673-3675`) returns an independent `Session(bind=db.engine)` and the `finally: session.close()` (`:772-773`, `:1009-1010`, `:1178-1179`) discards any open transaction on **every** path -- so deleting the rollback changes no persisted state anywhere, not merely on the path a test can reach. Both docstrings say so rather than papering over it. **The point worth carrying: "no test can kill this" and "no input can kill this" are different claims, and only the second justifies leaving a statement unpinned.** | recorded; the statements are covered, and the non-killability is explained rather than papered over | measured: Task 10's Step 13 found the outer handler in the community and feed tasks had been reached **only** by the five crash pins, so inverting them left `:1006-1008` and `:1175-1177` uncovered; `test_a_community_fetch_failing_outside_httpx_propagates` and `test_a_feed_fetch_failing_outside_httpx_propagates` re-cover it, one kill each. `get_task_session` and all three `finally` blocks read at this commit |

### 5. Three items deliberately NOT counted as defects

Recorded so nobody re-files them. Each was examined against source in this
sub-project and each has an explanation that survives reading.

- **`actor_data.close()` sits *inside* the `try` in `refresh_user_profile_task`
  (`:679`) and *after* it in the two handlers D214 added (`:806`, `:1042`).**
  The controller flagged it while reading the fix diff and the reviewer
  re-derived rather than accepted the judgement: on the decode-error path
  **neither** spelling closes -- the user task's `close()` is skipped by the
  exception, the new one by the `return` -- and `get_request` uses a
  non-streamed `httpx.get` whose body is fully read before it returns, so
  `close()` releases nothing `.json()` has not already released. The new shape
  is marginally the better one because its `try` covers less.
- **The `[deleted]` title normalisation in `refresh_user_profile_task`
  (`:726-727`) is not gated by an `in activity_json` check.** It reads
  `if user.title and user.title.strip().lower() == '[deleted]': user.title = ''`
  -- the *stored* value, not the document's -- so a refresh whose document
  carries no `name` key can still clear a title. That is coherent as a
  normalisation of the resulting value regardless of where it came from, and it
  is recorded only because it does **not** fit the optional-field branch-pair
  shape the rest of Task 4 covered, so a reader enumerating that block will find
  one branch that does not match its neighbours.
- **The community and feed tasks' second retry catch is `except Exception:`
  (`:797`, `:1033`) where the user task's is `except httpx.HTTPError:`
  (`:667`).** Genuinely broader, and it interacts with D220 -- but a broader
  catch on a *retry* that ends in `return` is defensible in a way the bare
  `except:` that ends in a *fallback fetch* is not, and lumping the two together
  would have made D220's entry claim more than it can support.

### 6. The final fix wave: four more registered, one of them the residual -- D232-D235

Added at commit `d43ccaad` (tests) and the register commit that follows it,
after the whole-branch review of D213-D231. **D232-D234 are peer-reachable
classes that none of the nineteen entries above covers**, each verified
against source at this commit rather than taken from the review's summary --
and **two of the three came back different from how the review described
them**, which is recorded here rather than quietly corrected, because the
difference is the finding in both cases. D235 is the residual coverage gap,
with the measurement, and it says plainly that the spec's first success
criterion was not met.

**The lesson the review drew, which the four must-fix items in
`tests/test_ap_refresh_profiles.py` all instantiate: a docstring sentence that
describes production structure in *prose* rather than in *identifiers* cannot
be audited by grep, and must be re-read against source whenever the code it
describes moves.** Every survivor was prose -- "the inner catch", "the guard
above", "the one gated collection", a bare function name. `tests/README.md`
fact 70 already says "grep the mechanism's identifier, not only the words used
to describe it"; this is its mirror image, and the more expensive half. The
fix-wave swept all 67 docstrings in the file by reading -- 66 function docstrings plus the module docstring, found **seven** wrong
(the four the review named plus three it did not: a guard count of three where
source has four, an unwrap attributed to the wrong one of two loops, and a
"THE LAST STATEMENT-COVERAGE GAP IN THIS SUB-PROJECT" that D235 measures as
false), and corrected all seven.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D232 | `refresh_feed_profile_task`'s following-collection fetch (`app/activitypub/util.py:1156`), against the eleven other `get_request` calls in the trio | **Not fixed -- the following-collection fetch is the ONLY `get_request` in all three tasks that sends no `Accept` header.** It reads `res = get_request(feed.ap_following_url)` with no `headers=` argument at all; every other call in the trio passes `headers={'Accept': 'application/activity+json'}` -- `:662`, `:666`, `:792`, `:796`, `:942`, `:980`, `:989`, `:1028`, `:1032`, `:1116`, and `signed_get_request` at `:672` separately. `get_request` with `headers=None` sets a `User-Agent` and nothing else (`app/utils.py:136-139`), so the peer sees a request that expresses no preference and is free to content-negotiate. **What makes this sharp is what D216's fix did to it.** Before that fix, a peer serving HTML at `/following` crashed the task with `JSONDecodeError` -- loud, attributable, and impossible to ignore. D216 wrapped that `.json()` in `except JSONDecodeError: res.close(); return`, so the same peer now returns **silently**, and the feed's `FeedItem` set never syncs: no exception, no log line, no `instance.failures` increment, and a feed that simply stops tracking its communities. **A fix whose entire job was to stop a crash converted it into a permanent silent functional failure**, and it did so only because this one call site was missing a header its eleven siblings all send. That is a general shape worth naming: adding a swallow to a call whose *inputs* are also wrong hides the input bug rather than fixing it. Mechanical to fix -- the correct spelling is on `:1116`, forty lines up, in the same function. Not fixed here only because this fix wave's authorisation forbade production changes. **NOW FIXED, at commit `a4a4f4b2` (sub-project 14, Task 11):** the call site passes `headers={'Accept': 'application/activity+json'}` like its siblings. The fix is pinned by `test_the_following_collection_fetch_asks_for_activity_json` in `tests/test_ap_refresh_profiles.py`, which asserts on the request respx RECORDED rather than on the outcome -- respx serves the route's canned response whatever the request asked for, so an outcome-only test passes either way. Measured while writing that pin: the peer did not see *no* `Accept` before the fix, it saw `*/*`, because httpx's own client defaults fill the header in when the caller sets none -- so a presence check would also have passed unfixed, and the pin is an equality against the intended value. Reverting the header alone fails that pin and no other test in the file. | **fixed at `a4a4f4b2`**, together with D219(c) at `c9cc56fc` -- the two mechanical ones, taken first by the follow-on slice as this entry directed | measured, by enumeration: `awk` over `:655-1180` listing every `get_request(` call at this commit gives the twelve sites above, and `:1156` is the only one with no `headers=`. The silent-failure consequence is read from D216's own fix (`:1157-1163`), whose `except JSONDecodeError` returns without touching `instance.failures` -- pinned, as a *correct* choice for a malformed body, by `test_a_malformed_following_collection_creates_no_feed_items`. No test drives an HTML-at-200 `/following` from a content-negotiating peer, which is the shape this entry is about |
| D233 | The kbin `moderators` arm in `refresh_community_profile_task` (`:811-812`) and `refresh_feed_profile_task` (`:1046-1047`), against the `attributedTo` arm one line above each | **Not fixed -- the `attributedTo` arm checks `isinstance(..., str)` and the `moderators` arm one line below it does not, in BOTH tasks.** The pair reads `if 'attributedTo' in activity_json and isinstance(activity_json['attributedTo'], str): mods_url = activity_json['attributedTo']` / `elif 'moderators' in activity_json: mods_url = activity_json['moderators']`. A peer sending `"moderators": {"type": "OrderedCollection", "id": "..."}` -- an object where the other arm's contract is a string -- assigns a `dict` to `Community.ap_moderators_url` (`app/models.py:605`, `db.String(255)`) or `Feed.ap_moderators_url` (`:4115`, same type). The commit that follows fails: psycopg cannot adapt a `dict` to a text parameter, so the flush raises out of `session.commit()` and the outer `except Exception: session.rollback(); raise` re-raises it. **Two of two wrong, with the correct spelling one line above each** -- the same mirrored-copy shape as D213 and D214, and the same mechanical repair (`and isinstance(activity_json['moderators'], str)`), which is why it is registered rather than left unnamed. **CORRECTION TO THE REVIEW'S DESCRIPTION, recorded because it changes what the entry claims:** the review called this "the partial-ingest shape the spec is built around", with profile fields "already applied" when the error lands. Read against source, it is not. In both tasks the assignment (`:826` / `:1055`) and the first commit of the block (`:897` / `:1108`) have **no commit between them**, so the rollback undoes every field the refresh set and nothing is persisted. The defect is a remotely-triggerable crash that loses the whole refresh, not a half-written row. **The genuine partial ingest in this family is elsewhere and is registered here for the first time:** the featured loop runs `UPDATE post SET sticky = false` **and commits it** (`:994-996`) *before* iterating `orderedItems` (`:997-998`), so any entry that is not a subscriptable object -- `item['id']` on a string raises `TypeError`, on a missing key `KeyError` (D219(a)) -- leaves every sticky post in the community un-stickied, committed, and the task raised. `session.rollback()` cannot undo a commit. | not fixed, registered only | reading-level, verified at this commit: both arm pairs read side by side (`:809-814`, `:1044-1049`); both column declarations read (`app/models.py:605`, `:4115`); the assignment-to-commit spans traced in both tasks to establish that the rollback is complete, and the featured loop's commit-then-iterate order read at `:994-998` to establish that one is not. Not covered: `_group_document`/`_feed_document` produce a string `attributedTo` or nothing, so no test in `tests/test_ap_refresh_profiles.py` sends an object under `moderators` |
| D234 | The four collection guards (`:947`, `:984`, `:993`, `:1121`) and the following guard (`:1166`), on the *type* of the container they admit | **Not fixed -- every guard in the trio checks key MEMBERSHIP and none checks container TYPE, so a JSON string passes and is iterated character by character.** `if following_collection and 'items' in following_collection:` is satisfied by `{"items": "https://evil/"}` -- `'items' in <dict>` is a key test -- and `for fci in following_collection['items']:` then yields `'h'`, `'t'`, `'t'`, `'p'`, ... one loop iteration per byte. The same holds for the two `orderedItems` membership loops (`:948`, `:1122`), the two `orderedItems` removal loops (`:967`, `:1144`) and the featured loop (`:997`). **CORRECTION TO THE REVIEW'S DESCRIPTION, and it is the more interesting reading:** the review called this "a 200-fetch amplification from a two-byte change", one outbound fetch per character via `find_actor_or_create`'s `create_if_not_found=True` (D226). **Traced through, that does not happen.** Each character does reach `find_actor_or_create` (`:280`), but `validate_remote_actor` (`app/activitypub/actor.py:39-83`) refuses it before `create_actor_from_remote` is reached: `extract_domain_and_actor('h')` (`:622-644`) hands `urlparse` a string with no authority and gets `netloc=''`, then `'://' not in 'h'` sends it to `normalise_actor_string` (`:4647-4657`), which returns `('', '')` for any string with no `'@'`, and `if not server: return False` ends it. So the fan-out is **zero fetches**, not one per character. What the character iteration *does* produce is real and worth the entry on its own: **(a) D225's `time.sleep(0.5)` is the FIRST statement of both membership loop bodies**, before `find_actor_or_create`, so it is paid per character regardless of the refusal -- a 200-byte URL string under `orderedItems` parks the worker for 100 seconds, which is D225's peer-controlled cost reached from a two-byte type change rather than from a genuinely long collection; **(b) any whitespace byte in the string crashes the task** -- `find_actor_or_create` does `actor.strip()`, `' '` strips to `''`, and `normalise_actor_string('')` reads `actor[0]` on an empty string (`:4650`) and raises `IndexError: string index out of range`; **(c) under `featured`'s `orderedItems` a string crashes with `TypeError` at `item['id']` (`:998`) after the un-sticky UPDATE has already committed**, which is the partial ingest described in D233. Not fixed because the repair is a design decision the register/fix line excludes -- `isinstance(..., list)` on five sites changes what the tasks accept from peers that today send a single-item collection unwrapped. | not fixed, registered only | reading-level, traced end to end at this commit rather than asserted: `find_actor_or_create` (`:280-310`), `validate_remote_actor` (`app/activitypub/actor.py:39-83`), `extract_domain_and_actor` (`:622-644`) and `normalise_actor_string` (`:4647-4657`) all read in sequence to establish that a one-character actor string is refused without a fetch, which is what falsified the review's amplification figure. Not covered: every collection this suite serves is a genuine JSON array |
| D235 | All three tasks, on the spec's first success criterion | **THE SPEC'S FIRST SUCCESS CRITERION -- full statement coverage of all three functions -- WAS NOT MET.** Stated plainly here because Task 11's own section above says "no per-function residual breakdown was re-measured at Task 11", and so left the criterion neither claimed nor disclaimed. It is now measured. From the coverage run at commit `979ef950`, **124 statements inside the three functions are uncovered**: `refresh_user_profile_task` **27** (`:664-668`, `:703`, `:717`, `:727`, `:733`, `:734`, `:736`, `:739`, `:748`, `:754-761`, `:769`, `:770`, `:771`, `:777`, `:778`, `:780`); `refresh_community_profile_task` **48** (`:818`, `:828`, `:830`, `:835`, `:840`, `:846`, `:849`, `:850`, `:855`, `:858-861`, `:863-871`, `:873-876`, `:878-886`, `:888-891`, `:893`, `:896`, `:937`, `:939`, `:955`, `:956`, `:969`, `:974`, `:977`); `refresh_feed_profile_task` **49** (`:1017`, `:1030-1034`, `:1047`, `:1053`, `:1061`, `:1063`, `:1068-1073`, `:1075`, `:1079-1082`, `:1084-1092`, `:1094-1097`, `:1099-1107`, `:1111`, `:1113`, `:1129`, `:1130`, `:1149`, `:1152`). **The shape of the gap is coherent, which is what makes it a scope rather than a list.** What the slice covered: the crash paths (D213-D217's five fixes and their pins), the envelope guards on all five collections, the retry and fallback paths, and the following loop. What it did not: **the document-application bodies** -- icon, image, description/summary/source, language, theme, flair -- which are largely untouched in two of the three tasks. The user task is the exception, because Task 4 walked its optional-field branch pairs one by one; the community and feed tasks got the same treatment for their *guards* and almost none for their *bodies*, and the two contiguous runs above (`:858-896` and `:1079-1107`) are exactly the mirrored icon/image/description blocks. **That is the coherent scope for a follow-on slice**, and it is a better-shaped one than this slice had: three copies of the same block, with the disagreements between them already enumerated as D219(b) and D219(d). **This is also the honest explanation for the blended figure.** The section above records that the spec predicted "toward 64%" and the measurement reached 61.8397%, and correctly warns that the 64.3106% *statement* figure is a different metric coincidentally near the prediction. The 124 statements are where the missing points went: they are the largest contiguous uncovered runs left in the three functions this slice owned. | not fixed -- registered as the scope for a follow-on slice | measured: the per-function statement lists above are from the coverage run at commit `979ef950`, the same run that produced the section's `61.8397%` blended module figure. **Not re-measured by this fix wave**, whose only test changes were to two existing tests and seven docstrings and which therefore cannot have moved these numbers materially -- the indexable pin now seeds a `Post`, which adds no statement to the three functions, and the local-feed pin still returns at the guard |

**Eight shapes worth carrying forward from this sub-project's rulings, now in
`tests/README.md` as facts 63-70:** a Celery task here is tested by calling it
directly, because production's own `current_app.debug` branch does the same;
`get_task_session()` leaves `autoflush` at SQLAlchemy's default `True` where
`db.session` is configured `autoflush=False`; a task committing on that session
leaves the test's own object stale, so `db.session.refresh()` **is** load-bearing
across sessions even though fact 58 shows it is a no-op within one;
`seed_community_owner` is not idempotent, because `Instance.domain` is unique;
an actor lookup inside the code under test fetches in two different ways, and
both need heading off; a fix that adds a conjunct must be mutation-proved by
deleting that conjunct alone; when two guards sit in sequence the pin for the
outer one must serve a payload the inner one accepts; and grep the mechanism's
identifier, not only the words used to describe it.

## Sub-project 14: the update pair's mirrored core

`docs/superpowers/specs/2026-09-04-coverage-update-pair-14-design.md` and
`docs/superpowers/plans/2026-09-04-coverage-update-pair-14.md` (design and plan;
the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-04-coverage-update-pair-14/`, not committed), on branch
`blentz`. Twelve tasks brought the mirrored core of two functions in
`app/activitypub/util.py` under test -- `update_post_reply_from_activity`
(`:3008-3133`, all of it) and `update_post_from_activity` (`:3136-3561`, its
scoped region only: the lock through `post.edited_at`, `:3136-3268`) -- and
fixed **eight** defects in eight
commits. Six are this slice's own (`5cb4f335`, `e9c38153`, `46c16385`,
`a7d40a3e`, `04386275`, `0df51342`, registered below as D236-D241) under this
sub-project's bounded, explicit authorisation; **two are sub-project 13's,
which registered them and named its follow-on as the place to take them**
(`a4a4f4b2` closing **D232**, `c9cc56fc` closing **D219(c)**). Those two
entries were updated in place in sub-project 13's own section at commit
`4ee4ee66` and **are not duplicated here** -- read them there, and note that
D232 now carries an appended correction of its own. Task 12 closed the twelve
tasks out with the findings register, the test-harness log, and the coverage
floor; **a final, documentation-only fix wave then closed the sub-project**,
disposing the whole-branch review's three must-fix items and adding
**D252-D259** in subsections 4 and 5. That wave changed no production code and
no test's assertions, fixtures or bodies -- two false docstring citations
corrected, three asymmetry-table rows discharged by registration, and five
peer-reachable crashes registered. Where it changes what an earlier paragraph
or entry claims, the change is **appended and marked**, per this file's
convention (D233, D234, D243), rather than applied silently. Tests live in `tests/test_ap_update_pair.py` (106 test
functions, none parametrized). `app/activitypub/util.py` measures **66.8156%
blended** (68.6261% statements, 1973/2875, 902 missing; 63.5625% branches)
after this sub-project, up from 61.8397%; the floor rises **61 -> 66**. Full
suite after this sub-project: **3531 passed, 3 skipped, 6 subtests passed** in
225.54s, against 3423 at the end of sub-project 13 -- the branch gained **108**
tests.

**THE SPEC'S FIRST SUCCESS CRITERION WAS MET, and that is the headline
difference from sub-project 13.** Measured out of the coverage JSON at this
slice's last commit, using the function bounds **as they now stand** (the eight
fixes shifted them): `update_post_reply_from_activity` `:3008-3135` has **zero**
uncovered statements -- the `def` runs `:3008-3133` and the window includes the
two blank lines after it -- and `update_post_from_activity`'s scoped region from
`:3136` has **zero**. One nearby missing line, `:3005`, sits inside
`notify_about_post_reply` and is outside the region; it was checked rather than
assumed, because a first query using the *spec's* stale bounds reported it as a
hit inside the region.

**Sub-project 13 stated the same criterion against whole functions, missed it
by 124 statements, and only discovered that at its final review** (D235). The
difference is not effort and it is not luck: **this spec scoped the criterion to
a region that could actually be finished, and a criterion scoped to something
finishable is a criterion that can be checked.** That is the strongest evidence
this campaign has produced for stating success criteria against a bounded region
rather than a whole unit, and it is worth more than either sub-project's
coverage number.

**The fix rule, unchanged from sub-project 13 and load-bearing again here: a
defect is fixed under a bounded authorisation when the correct spelling already
exists in the file and the change is mechanical; it is registered when the fix
would require choosing new behaviour for a case the codebase has never
handled.** All six fixes met the first test -- four copy a sibling guard in the
mirrored twin, one copies the create path's own loop structure verbatim, and one
switches a consumer to the relationship spelling two of its three siblings
already use. Nine items are registered rather than fixed, each with its reason
stated in its own row. **Two of the nine are out-of-scope twins of defects that
were fixed in scope** (D243, D245), which is the one-clause-several-sites gap
this campaign keeps hitting; both name their site so the next slice fixes it
first.

**Two rulings were reversed mid-slice, and both reversals are worth more than
the original rulings.** Rule 3's dead `continue` was first registered on the
grounds that no sibling showed the intended control flow -- then the Task 5
reviewer found the working twin 300 lines above, in `create_post_reply`, and the
defect became a mechanical re-indent (D241). **The lesson: before ruling a
defect register-only for want of a correct spelling, grep the file for the
twin.** A mirrored pair is not the only place a sibling can live. The same grep,
run against the same-document Mention duplication, came back empty -- and that
one stayed registered (D242), which is what makes the first reversal a rule
rather than an excuse.

**One root cause produced three separate defects, and the register should be
read that way**: `db = SQLAlchemy(session_options={"autoflush": False}, ...)`
(`app/__init__.py:81`) makes a pending write invisible to a later read in the
same request. D239 is that fact hitting `find_language_or_create`'s unflushed
row -- twice, with two different wrong outcomes. D242 is the same fact hitting
`existing_notification` inside a single tag loop. Neither is a defect in
`autoflush=False`; both are consumers written as if it were `True`.

**And one family runs through the whole slice**: `request_json[...]` and
`json_tag[...]` subscripts on peer-controlled documents with no membership
check. Four were found. Three are in scope and fixed (D236, D238), one is past
the scoped region and registered (D245); two more of the same shape are
registered because they sit outside what this slice's tests can prove (D246,
D247). Sub-project 15 inherits all three of the registered ones, and **D245
gates every test that slice will write, exactly as it gated this one's**.

**CORRECTION TO THIS PARAGRAPH'S COUNT, appended rather than rewritten because
it changes what the paragraph claims.** "Four were found" was true when the
twelve tasks closed. The whole-branch review then found **five more inside the
scoped region**, registered by the final fix wave as D255-D259 in subsection 5
below, so the family stands at **nine** and the registered-and-inherited count
rises from three (D245, D246, D247) to **eight**. The correction is worth more
than the arithmetic: **the four found in-slice were all found by the sibling
comparison, and three of the five found afterwards are SYMMETRIC** -- D256 and
D257 are wrong in the same way on both halves, and the comparison that found
everything else is blind to those by construction. D247 predicted exactly this
and named itself as the sole instance; it now has company, which promotes the
prediction from an observation to a method rule stated in D256.

### 1. Six defects fixed, test-first with mutation-proved tests -- D236-D241

Each was pinned by a test asserting the *crash* or the *wrong write* first, the
pin then inverted to assert the fixed behaviour, and each fix mutation-proved at
every call site separately. Every one is **remotely triggerable by any peer
sending an `Update`**, which is why they were fixed rather than registered.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D236 | `update_post_from_activity`'s Markdown-source arm (`app/activitypub/util.py:3141-3144`), against `update_post_reply_from_activity`'s (`:3015-3016`) | **FIXED, commit `5cb4f335`. The post function read `request_json['object']['source']['mediaType']` with no membership check, so a peer sending a `source` object carrying `content` but no `mediaType` raised `KeyError` out of the function and the whole `Update` was lost.** The reply twin has always read `'source' in … and isinstance(…, dict) and 'mediaType' in … and …['mediaType'] == 'text/markdown'`; the post copy had the first two conjuncts and not the third. The fix inserts `'mediaType' in request_json['object']['source'] and` in the position the reply function has it -- one line, the sibling's spelling exactly. **The fix VACATED A BRANCH, which is the finding inside the finding**: adding the membership conjunct means a *string* `source` no longer kills the `isinstance(…, dict)` mutant, because `'mediaType' in 'not a dict'` is a **substring** test returning `False` rather than raising, so the guard short-circuits identically with or without `isinstance`. The existing string test PASSED under the mutant after the fix. The post-side twin of the reply suite's `source=None` fixture was added to restore that kill, and the string test's docstring -- which asserted in prose that this guard had no membership conjunct -- was corrected in the same commit. | **fixed**, commit `5cb4f335` | measured: `test_a_post_source_with_no_media_type_is_skipped`, written as a `pytest.raises(KeyError)` pin and inverted, pre-fix failure `KeyError: 'mediaType'` at `util.py:3134` (pre-fix numbering); plus `test_a_post_none_source_does_not_leak_past_the_dict_check`, added by this fix to kill the vacated `isinstance` mutant (`TypeError: argument of type 'NoneType' is not iterable`) |
| D237 | `update_post_reply_from_activity`'s content gate (`app/activitypub/util.py:3011`), against `update_post_from_activity`'s (`:3140`) | **FIXED, commit `e9c38153`. The reply function gated on `'content' in request_json['object']` alone, so a peer sending `"content": null` -- which Mastodon and Lemmy both do on some edits -- raised `AttributeError: 'NoneType' object has no attribute 'startswith'` out of the function.** The post twin has always read `'content' in … and …['content'] is not None`. The fix appends ` and request_json['object']['content'] is not None` -- the sibling's spelling, character for character. **This is the pair's asymmetry running the other way from D236**: the post function was right here and wrong there, which is precisely why the pair was worth taking as one slice. The post-side docstring that described this as an asymmetry the reply function was on the wrong side of was corrected in the same commit that made it false. | **fixed**, commit `e9c38153` | measured: `test_a_reply_with_null_content_keeps_its_body`, pre-fix `AttributeError: 'NoneType' object has no attribute 'startswith'` at `util.py:3009` (pre-fix numbering); mutation B1 (conjunct deleted) kills it alone |
| D238 | `update_post_from_activity`'s tag loop, the `Hashtag` and `lemmy:CommunityTag` comparisons (`app/activitypub/util.py:3216` and `:3222`), against the `Mention` comparison three lines below (`:3228`) and the reply twin (`:3066`) | **FIXED, commit `46c16385`. Two of the three `json_tag['type']` reads in the post's tag loop had no membership check, so a peer including one tag entry with no `type` key raised `KeyError: 'type'` and abandoned the loop -- after `post.tags.clear()` had already run, so the post lost every tag it had.** One defect, two call sites, each mutation-proved separately. The third comparison in the same loop (`:3228`) and the reply function's single comparison (`:3066`) both already read `'type' in json_tag and json_tag['type'] == …`; the fix copies that into the two that lacked it. **The fix made a previously-unkillable clause killable, and the irony is exact**: sub-project 14's Task 9 proved `'type' in json_tag` on the `Mention` comparison **unkillable** because the two unguarded subscripts above it meant the key provably existed by the time it ran -- it was unkillable *only because of the defect this fix removes*. The fix commit therefore had to add the test that kills it, and did. **One behaviour widening is recorded rather than missed**: a bare *string* in the tag list previously raised `TypeError` at the `Hashtag` arm and now silently skips, because `'type' in 'x'` is a substring test -- but the `Mention` arm already behaved that way, so the fix makes the loop **uniform** rather than introducing a new tolerance. | **fixed**, commit `46c16385` | measured: `test_a_post_tag_entry_with_no_type_key_is_skipped`, one pin killing all three mutants separately (C1 `Hashtag`, C2 `lemmy:CommunityTag`, C3 the newly-reachable `Mention` conjunct); its assertions are a real `Hashtag` entry *after* the typeless one plus exact-length equality, because `post.tags.clear()` runs unconditionally and a bare "tags still empty" assertion cannot distinguish "skipped the entry" from "abandoned the loop" |
| D239 | `find_language_or_create` (`app/activitypub/util.py:384-397`) and its two consumers -- `update_post_reply_from_activity` (`:3022-3028`) and `update_post_from_activity` (`:3197-3207`) | **FIXED, commit `a7d40a3e`. `find_language_or_create` returns a row it has only `add()`ed, never flushed, so `.id` is still `None` when the caller reads it -- and the two language consumers were the only `*_or_create` consumers in these functions that read `.id` instead of assigning the relationship. ONE unflushed read, TWO distinct wrong outcomes, and which one fires depends on whether the row already had a language.** *(a)* The reply consumer did `reply.language_id = language.id` unconditionally, so a peer sending a language code this instance has never seen **created the `Language` row and wrote NULL into the reply** -- silent data loss, no crash, nothing in production surfaces it. *(b)* The post consumer's guard read `if new_language and (new_language.id != old_language_id)`, so the same unflushed `None` produced a NULL write when the post **had** a language, and `None != None` being `False` meant the assignment was **skipped entirely** when it did not -- a brand-new language code silently dropped. The correct spelling was already in the file and used by two of the three consumers of an unflushed `*_or_create` result: the tag and flair arms do `post.tags.append(hashtag)` / `post.flair.append(flair)` and let SQLAlchemy resolve the id at flush, and **both models declare the relationship** -- `Post.language` (`app/models.py:1769`), `PostReply.language` (`app/models.py:2937`). All four `*_or_create` helpers in this file add without flushing, so the *helper* is a house pattern and changing it would be a design change; the *consumers* disagree, and that is the asymmetry the fix corrects. **The relationship assignment alone was not sufficient on the post side**, and that is worth stating: `post.language = new_language` still short-circuits when both operands are `None`, so the fix also adds the disjunct `new_language.id is None or`, making "id is `None`" mean "brand new" rather than "same as a post with no language". Verified by table across all six combinations of (post has a language / does not) x (resolved language is new / existing / `None`): both broken cells now correct, the four already-correct cells unchanged, and the `is None` disjunct proved unable to fire spuriously, since it is true only for an unpersisted row and for an unpersisted row assignment is always what is wanted. **See D250 for the new asymmetry this fix creates.** | **fixed**, commit `a7d40a3e` | measured: `test_a_reply_language_new_to_this_instance_is_created_and_applied` (pre-fix `assert None == 1`) and `test_a_post_language_new_to_this_instance_is_created_and_applied` (pre-fix `assert None == 2`), both asserting against the real `Language` row fetched back rather than against "not None"; the post pin starts from `assert post.language_id is None` so symptom *(b)* is the one it exercises. Both tests were **undiscoverable before the fix** -- under the bug the no-language case is invisible because both sides of the comparison are `None` -- so Tasks 2 and 8 pre-seeded and committed the `Language` row and said so in their docstrings; that workaround was removed here |
| D240 | `update_post_reply_from_activity`'s de-duplication rule 4 (`app/activitypub/util.py:3106-3108`) | **FIXED, commit `04386275`. `ids = tuple(ids)` followed by `SELECT user_id FROM "post_reply" WHERE id IN :ids` with no guard: for a reply whose `path` contains no ancestors the tuple is empty, psycopg2 renders `IN ()`, and Postgres rejects it -- `sqlalchemy.exc.ProgrammingError: (psycopg2.errors.SyntaxError) syntax error at or near ")"`.** The reachable trigger, traced from function entry to the query rather than assumed: **every edit of a top-level microblog comment carrying two or more tags that Mentions a local user who is not the post's author**, and for whom no `post_mention` notification for that post already exists. Narrower than "every top-level Mastodon or mbin comment that mentions a local user" -- an earlier and looser statement of the scope was corrected in `d8c758a5` -- and still neither rare nor exotic. The repair is a mechanical `if ids:` around the query and is behaviour-preserving against intent: with no ancestor ids there is nothing to suppress, so an empty result and a skipped query reach the same outcome and only the crash differs. **A consequence was predicted at Task 5 and confirmed twice by mutation**: the loop's `element == 0` disjunct is now permanently unkillable, because the sentinel `0` was visible only *through* the crash and with the guard it merely widens two `IN` lists that nothing matches -- `post_reply.id` starts at 1 and no `Notification` carries `comment_id` 0. That is **unreachable data**, not unreachable behaviour: no fixture could kill it without inserting a `PostReply` with id 0, which fabricates a state production cannot reach and would be a fake kill. No test was invented. **See D243 for the identical unguarded site in the create path, which is NOT fixed.** | **fixed**, commit `04386275` | measured: `test_a_top_level_microblog_reply_mention_skips_the_ancestor_lookup`, originally written as a `pytest.raises(ProgrammingError)` crash pin and **rewritten into a three-column positive assertion in the same commit**, because left as `pytest.raises` it fails the moment the guard lands. Pre-fix failure quoted verbatim in the task report including `LINE 1: SELECT user_id FROM "post_reply" WHERE id IN ()`; mutation E1 (guard removed and body dedented) kills it alone |
| D241 | `update_post_reply_from_activity`'s de-duplication rule 3 (`app/activitypub/util.py:3090-3101`), against the working twin in `create_post_reply` (`:2722-2733`) | **FIXED, commit `0df51342`. Rule 3 -- "ignore a Mention mirroring one someone else already made in the comment chain" -- was DEAD CODE and never suppressed anything.** The `comment_mention` query and its `if notifs: continue` were indented **inside** the `for element in reply.path` loop, so the `continue` advanced a loop that was about to advance anyway and `notifs` was never read afterwards. Proved algebraically and then by two surviving mutants -- `continue` -> `pass`, and deleting the query and guard outright, both green. **The correct implementation already existed in the file, 300 lines above**: `create_post_reply`'s copy of the same block (`:2722-2733`) has the loop containing only the skip and `ids.append`, with the query and its `continue` at the same indent as rules 1, 2 and 4, where the `continue` genuinely skips the enclosing iteration. The update path is a **botched copy of a working sibling**, not a design choice, and the repair is a mechanical re-indent that de-diverges the two. It is a real behaviour change -- this instance stops sending duplicate chain mentions it currently sends -- but it is the behaviour the author demonstrably intended, which is exactly what the create path proves. Two further consequences the reviewer spelled out and the report did not: `ids` is now **complete** when `.in_(ids)` is built, where the old code ran the query once per surviving path element against a partially built list and discarded every result; and for a top-level reply `.in_([])` renders as a valid always-false predicate, so D240's case still falls through correctly. | **fixed**, commit `0df51342` | measured: `test_a_microblog_reply_mention_mirroring_a_comment_mention_in_the_chain_is_suppressed`, pre-fix `AssertionError: assert 1 == 0` -- the notification that rule 3 was supposed to suppress. The pin's suppression is **attributable rather than incidental**: the seeded row's url is the *ancestor's*, so the later `existing_notification` check cannot be the cause, and the ancestors are authored by the remote author, so rule 4 cannot be either |

### 2. Nine items registered, not fixed -- D242-D250

Each carries the reason it was not fixed. D243 and D245 are the out-of-scope
twins of D240 and D236/D238 and are the two the next slice should take first;
D245 in particular **gates every test sub-project 15 will write**.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D242 | Both functions' Mention notification blocks -- `update_post_reply_from_activity` (`app/activitypub/util.py:3114-3116`) and `update_post_from_activity` (`:3236-3238`) | **Not fixed -- a document carrying the SAME Mention tag twice produces TWO notification rows.** The `existing_notification` query filters `user_id` **and** `url` and runs through the ORM with no flush, and the app factory sets `session_options={"autoflush": False}` (`app/__init__.py:81`), so the first tag's pending `db.session.add` is **invisible** to the second tag's query. There is no `break` anywhere in either mention loop -- a Task 4 docstring claiming "the block breaks out per tag" was false and was corrected in `d8c758a5`. **Same root cause as D239**: a pending write invisible to a later read in the same request. Not fixed because, unlike D241, there is **no correct twin** -- both copies of the block share the defect, so the file demonstrates no intended spelling -- and the repair requires *choosing* a mechanism: a flush after the `add`, an in-memory seen-set across tags, or de-duplicating the tag list before the loop. Each has different consequences for the surrounding session and nothing in the file selects between them. That is the register side of this slice's own fix rule, applied to a case where the grep-for-the-twin lesson was followed and came back empty. **Cost if wrong: a peer can inflate a local user's notification count by repeating a Mention tag, documented and unfixed for one more slice.** | not fixed, registered only | confirmed empirically by the Task 5 fix-round implementer by running it, not by reading; the cross-document case is separately pinned by `test_a_second_reply_mention_does_not_duplicate_the_notification` and `test_a_second_post_mention_does_not_duplicate_the_notification`, which pass and are not affected |
| D243 | `create_post_reply`'s Mention de-duplication rule 4 (`app/activitypub/util.py:2738-2739`), the create-path twin of D240 | **Not fixed -- the identical unguarded `ids = tuple(ids)` / `WHERE id IN :ids` sits in the create path and is a live `ProgrammingError` on CREATING a top-level microblog comment that Mentions a local user other than the post author.** Character-for-character the site D240 fixed, with the same preceding loop skipping `0` and the reply's own id. Not fixed because the site is outside this slice's authorisation -- the spec lists `create_post_reply` as out of scope and this slice owns no tests for it -- and because **the campaign's binding rule is that every fix is proved by a mutation failing a NAMED test**. There is no test at that site and this slice would not write one, so a fix there could not be proved, and an unproven production change is worse than a registered one. This is the one-clause-several-sites gap the campaign has now hit five times, accepted deliberately and recorded rather than walked into. **The site is named explicitly so the next slice fixes it first. Cost if wrong: an identical verified crash stays one slice longer in a function this slice cannot prove a fix against.** **CORRECTION TO THE LEDGER'S OWN RELOCATION, recorded because it changes what the entry claims:** the Task 10b record relocated this twin from `create_post_reply` to `notify_about_post_reply` and instructed the register to cite the latter. Verified against source at this commit, that is wrong. `ids = tuple(ids)` and `SELECT user_id FROM "post_reply" WHERE id IN :ids` occur at exactly two places in the whole file -- `:2738-2739` and `:3106-3108` -- and `:2738` lies inside `create_post_reply` (`:2593-2771`). `notify_about_post_reply` (`:2936-3005`) contains neither string. The controller's earlier attribution was right and the relocation was the error; the line number moved (`:2736` -> `:2738`) because the eight fixes shifted the file, the function did not. **This is exactly the two-claim citation the constraint on this task exists to catch: "*code* inside *function* (`:N`)" carries a line claim and a function claim, and here the line claim was stale while the function claim was false.** **CLOSED BY SUB-PROJECT 15, commit `3989f55b`, appended rather than rewritten because it changes this entry's status.** The fix is the `if ids:` guard D240 put on the identical site, copied character for character: `ids = tuple(ids)` at `app/activitypub/util.py:2740` is now followed by `if ids:` (`:2741`) wrapping the query and its `continue` (`:2742-2744`), with the reason in a two-line comment above (`:2738-2739`). Sub-project 15 owned the tests this slice did not, so the fix is mutation-proved as the campaign's rule requires. **The prediction in this entry that the crash was "live" understated it in one direction and overstated it in another, and both are worth recording.** Understated: `create_post_reply`'s tail `except Exception as ex` (`:2769-2771`, now **D263**) *swallowed* the `ProgrammingError` into a `None` return, so the symptom in production was not a visible crash but a **silently missing notification on a reply row `PostReply.new` had already committed** -- which is why this copy went unnoticed a slice longer than D240's. Overstated: nothing else. **The fix cost two disjuncts, exactly as D240's did** -- `element == 0` and `element == post_reply.id` in the skip loop (`:2725`) are now permanently unkillable, killable only *through* the crash, and no test was invented for them. **And it UNKILLED an existing mutant, which is fact 74 arriving as a live regression in proof**: forcing the microblog gate (`:2708`) True had been killed pre-fix by five tests that route around this crash -- through the crash, not through any rule -- and survived post-fix at 67 passed; `test_a_non_microblog_mention_of_the_post_author_is_delivered` was added in the same commit to pin the gate's False side on its own merits. | **FIXED by sub-project 15**, commit `3989f55b` (was: not fixed, registered only) | reading-level, verified against source at this commit by the Task 12 implementer: a `grep -n` for `IN :ids` returns exactly two lines, `:2739` and `:3108`, and an `ast` walk of the module assigns `:2739` to `create_post_reply` (`:2593-2771`) and `:3108` to `update_post_reply_from_activity` (`:3008-3133`). **Closure evidence:** `test_a_top_level_microblog_mention_skips_the_ancestor_lookup` in `tests/test_ap_create_reply.py`, written by Task 6 as a crash pin **to be inverted** and inverted in the fix commit, because left as a `pytest.raises` it fails the moment the guard lands; Task 6 proved it invertible in advance by applying the guard as its M18 and watching the pin fail |
| D244 | `update_post_reply_from_activity`'s tag gate (`app/activitypub/util.py:3064`), against `update_post_from_activity`'s (`:3210`) | **Not fixed -- the reply function ignores a LONE Mention.** Its gate reads `'tag' in … and isinstance(…, list) and len(request_json['object']['tag']) > 1`, so a peer sending exactly one tag -- a single `Mention`, which is the ordinary shape of a Mastodon reply that mentions one person -- is skipped entirely and nobody is notified. The post function's gate has no length condition at all. Registered rather than fixed because removing the length term **changes which notifications this instance generates** for a case that has always been silent, which is behaviour rather than a guard, and because nothing in the file explains what the `> 1` was for. **Cost if wrong: single-mention reply edits keep producing no notification, documented.** | not fixed, registered only | pinned as current behaviour by `test_a_lone_reply_mention_is_ignored`, which asserts no notification row; the gate's third conjunct is mutation-covered by that test |
| D245 | `update_post_from_activity`'s type dispatch (`app/activitypub/util.py:3270`) | **Not fixed -- `request_json['object']['type']` is read UNCONDITIONALLY, so a peer sending an `Update` whose object carries no `type` key raises `KeyError: 'type'` out of the function.** It is the fourth member of D236/D238's unguarded-peer-subscript family and the only one outside this slice's region: the scoped region ends at `post.edited_at` (`:3268`) and everything from `:3270` is assigned to sub-project 15. Not fixed for the same reason as D243 -- out of scope means registered, and a fix there could not be mutation-proved by a slice that owns no tests for that region. **This is the FIRST thing sub-project 15 will see, and it should be fixed before that slice writes anything else, because it gates every test that slice will write -- exactly as it gated this one's.** Every test in Task 6's brief omitted `type` and every one of them crashed on it; the whole post-side suite threads `type='Note'` through its payloads as a result, and says so in the module docstring. **Cost if wrong: an unguarded subscript on peer data stays one slice longer, documented, with its discovery already paid for.** | not fixed, registered only | reading-level, verified against source at this commit; the workaround is visible in every post-side `_update(...)` call in `tests/test_ap_update_pair.py` and documented in that file's module docstring |
| D246 | `update_post_from_activity`'s `Hashtag` arm (`app/activitypub/util.py:3217` and `:3219`) | **Not fixed -- `json_tag['name']` is read with no membership check, so a peer sending `{"type": "Hashtag"}` with no `name` key raises `KeyError: 'name'` out of the function, after `post.tags.clear()` has already run.** Same shape as D238 and immediately behind the guard D238 added: the fix put `'type' in json_tag` in front of the comparison, and the *body* it admits then subscripts `name` twice unguarded. Not fixed because it is outside what D238's authorisation covered -- the spec named the `type` reads specifically -- and because deciding what to do with a nameless `Hashtag` (skip the entry, skip the loop, keep the cleared tags) is a behaviour choice the file does not make anywhere. **Cost if wrong: a fifth unguarded peer subscript in the same loop, documented, one slice longer.** | not fixed, registered only | reading-level, found by the Task 10a reviewer and verified against source at this commit; noted there that the `KeyError` behind the comparison is still what a `Hashtag`-arm mutant produces |
| D247 | `update_post_from_activity` (`app/activitypub/util.py:3145`) and `update_post_reply_from_activity` (`:3017`) | **Not fixed -- `request_json['object']['source']['content']` is read unguarded on BOTH sides, so a `source` dict carrying `"mediaType": "text/markdown"` and no `content` raises `KeyError: 'content'` out of either function.** Symmetric, so it is **not** a pair asymmetry and the sibling comparison that found every other defect in this slice cannot find it -- which is why it is recorded here explicitly rather than left to be re-derived. It is the same defect family as D236, D238, D245 and D246. Not fixed because there is no correct spelling anywhere in the pair to copy: both copies read it the same way, and choosing between "skip the Markdown arm" and "fall through to the HTML body" is behaviour. **Cost if wrong: one more remotely-triggerable `KeyError`, on both halves of the pair, documented.** | not fixed, registered only | reading-level, found by the Task 10a reviewer and verified against source at this commit |
| D248 | `update_post_from_activity`'s Mention notification (`app/activitypub/util.py:3247`), against `update_post_reply_from_activity`'s (`:3124`) | **Not fixed -- only the reply path localises the notification to the RECIPIENT.** The reply path wraps the `Notification` construction in `with force_locale(get_recipient_language(recipient.id)):`; the post path builds its title with a bare `_()`, under whatever locale the request is being handled in -- which, on an inbox request, is the locale of a remote peer's delivery, not of the person being notified. **A local user mentioned in a post can therefore be notified in another user's language.** Registered rather than fixed because it is a user-visible behaviour change in a subsystem this slice has no i18n tests for, and because the campaign's fix rule wants a *mechanical* copy: wrapping the post block would move a `Notification(...)` construction and a counter increment inside a new context manager, which is a restructure rather than a guard. **Cost if wrong: mention notifications on posts stay mis-localised, documented.** | not fixed, registered only | reading-level, named in the spec's asymmetry table while scoping and verified against source at this commit |
| D249 | The two locks -- `update_post_reply_from_activity` (`app/activitypub/util.py:3010`) and `update_post_from_activity` (`:3138`) | **Not fixed -- the two locks differ by 6x and 10x with no stated reason.** The reply takes `redis_client.lock(f"lock:post_reply:{reply.id}", timeout=10, blocking_timeout=6)`; the post takes `redis_client.lock(f"lock:post:{post.id}", timeout=60, blocking_timeout=60)`. A slow `Update` on a reply gives up where the same work on a post waits, and the reply function is the one whose Mention block issues up to three extra queries per tag. Registered rather than fixed because nothing in the file says which number is the intended one, and picking either changes production timing behaviour under contention -- the clearest possible case of "choosing new behaviour for a case the codebase has never handled". **Cost if wrong: an undocumented 10x difference in give-up time survives, documented.** | not fixed, registered only | reading-level, named in the spec's asymmetry table while scoping and verified against source at this commit; note that `redis_double` cannot exercise it -- fakeredis has no Lua, so `Lock.release()` raises on `__exit__`, and this file uses a lock double instead |
| D250 | `update_post_from_activity`'s language guard (`app/activitypub/util.py:3206`), against `update_post_reply_from_activity`'s unconditional assignment (`:3028`) | **Not fixed -- D239's fix DEEPENED the pair's language asymmetry rather than resolving it, and this entry exists so that is on the record rather than discovered later.** The reply side assigns unconditionally inside its `language` dict guard; the post side now reads `if new_language and (new_language.id is None or new_language.id != old_language_id):` -- three ways to be wrong where it previously had two. All three are mutation-covered (D2, D3, D4 in the Task 10b table), so the code is proved; the point is structural. Registered rather than converged because the post side's "assign only on a change" rule is real behaviour the reply side does not have, and deciding which half is right is a design question. **If the two halves of this pair are ever converged, this is the asymmetry to decide about first. Cost if wrong: nothing today -- this is a signpost, not a live defect.** | not fixed, registered only | flagged by the Task 10b implementer in its own concerns and verified against source at this commit; `test_an_unchanged_post_language_is_not_reassigned` remains a documented non-killer, since making the guard unconditional writes the identical value |

### 3. One coverage gap registered, not pinned -- D251

Added at commit `9fd6b93c`'s fix round, disposing an item Task 3's review
deferred to this sub-project's final review. It is not a defect and not a
production change; it is a decision nobody has made, recorded so the next slice
does not re-derive it.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D251 | `update_post_reply_from_activity`'s attachment block (`app/activitypub/util.py:3039-3056`) | **Not fixed and deliberately NOT pinned -- a SCALAR `attachment` matches neither `isinstance(..., dict)` nor `isinstance(..., list)`, so it is silently discarded.** A peer sending `"attachment": "https://example/img.png"` -- or any bare string, number or boolean -- leaves `attachment_list` at `[]` (`:3040`), the `for attachment in attachment_list:` loop (`:3045`) runs zero times and the `if attachment_list:` regeneration gate (`:3055`) no-ops, so the reply's `body` and `body_html` are unchanged and nothing is raised. Traced by Task 3's reviewer, not inferred. **Not a defect**: it is a benign skip, neither a crash nor silent corruption, which is why it is a coverage gap rather than a register entry of the D236 family. **Not pinned, ruled at the final review**: a test could only assert *nothing happened*, which is the weakest assertion shape this campaign accepts, and pinning it would **freeze behaviour nobody has decided** -- whether a scalar `attachment` *should* be accepted (coerced to a one-element list, as some peers would expect) is a question for whoever owns that contract, not a guard this slice can prove. The post function has no attachment handling in its scoped region, so there is no sibling to vote. **Cost if wrong: a benign no-op stays untested, documented.** | not fixed, not pinned, registered only | reading-level, traced by the Task 3 reviewer and verified against source at this commit; the `dict` and `list` arms are each pinned by their own tests (`test_a_reply_single_attachment_dict_is_appended`, `test_a_reply_attachment_list_is_appended_in_order`), and `test_an_empty_attachment_list_does_not_regenerate_the_html` covers the empty-list path the scalar case collapses into |

### 4. Three asymmetry-table rows discharged by registration -- D252-D254

Added by the final fix wave, after the whole-branch review found them
undischarged. **The spec's sixth success criterion requires every row of its
asymmetry table to be EITHER fixed with a mutation-proved test OR registered
with a stated reason.** Seven of the ten rows were discharged: rows 1, 2 and 7
by fixes (D236, D237, D238), rows 5, 6, 8 and 10 by entries (D250, D244, D248,
D249). **Rows 3, 4 and 9 were pinned by tests and then neither converged nor
registered, and that is the gap these three entries close.** The lesson is
worth as much as the entries: **a row covered by a test is not a row
discharged.** All three were covered from Task 3 onward -- every arm of row 3
has its own test, every observable of row 4 has one, and all four rules of row
9 do -- which is exactly why nobody noticed they had never been *decided*.
Coverage answers "does the code do what it does"; the criterion asks "has
somebody ruled on whether the two halves should differ", and a green test
answers neither half of that question.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D252 | `update_post_from_activity`'s object-level `mediaType` chain (`app/activitypub/util.py:3147-3152`), against `update_post_reply_from_activity`'s content block (`:3011-3020`) | **Not converged -- the post function dispatches on an object-level `mediaType` and the reply function has no counterpart, so ONE `Update` document produces two different bodies depending on which row it targets.** The post block is a four-arm chain: `source`/`text/markdown` (`:3142-3146`), `elif … request_json['object']['mediaType'] == 'text/html'` (`:3147-3149`), `elif … == 'text/markdown'` (`:3150-3152`), and an `else` that wraps and allowlists (`:3153-3157`). The reply block has two arms and no chain: it wraps and allowlists **unconditionally first** (`:3012-3014`), then takes the `source` markdown arm or falls through to `html_to_text` (`:3015-3020`). A peer sending `"mediaType": "text/markdown"` at object level gets `markdown_to_html(content)` on a post and `allowlist_html('<p>' + content + '</p>')` on a reply -- markdown rendered on one, escaped as literal text on the other. **Row 3 of the spec's asymmetry table; this entry discharges it.** Not converged because **neither direction is mechanical and neither is a guard**, which is precisely the register side of this slice's fix rule. Copying the two `elif` arms into the reply function cannot be done as a copy: the reply block performs its wrap and its `allowlist_html` *before* its arms, so the arms would have to be restructured around that, and doing so changes the body every peer that sends the field gets -- new behaviour for a case the reply function has never handled. Deleting the arms from the post side removes a live capability that three tests currently pin. Nothing in the file votes. **Cost if wrong: the pair keeps deriving two different bodies from one document shape, documented.** | not converged, registered only | reading-level, both blocks read side by side at this commit; pinned as current behaviour on the post side by `test_a_post_html_media_type_allowlists_the_content`, `test_a_post_html_media_type_skips_the_wrap_that_else_would_apply`, `test_a_post_markdown_media_type_renders_the_content` and `test_a_post_unrecognized_media_type_falls_through_to_the_wrap`, which cover all four arms. The reply side has no object-level `mediaType` test **because it has no such arm** -- the absence is the asymmetry, not a coverage gap |
| D253 | `update_post_from_activity`'s `contentMap` language fallback (`app/activitypub/util.py:3200-3201`), against `update_post_reply_from_activity`'s language block (`:3022-3028`) | **Not converged -- only the post path falls back to `contentMap` for a language, and D250 does NOT cover this.** D250 registers the *other* half of the language asymmetry -- the post side's `new_language.id is None or new_language.id != old_language_id` comparison before assigning (`:3206`) -- which is when the language is applied. This row is **where the language comes from**, a separate axis, and it was left undischarged. The post side reads `elif 'contentMap' in request_json['object'] and isinstance(request_json['object']['contentMap'], dict): new_language = find_language(next(iter(request_json['object']['contentMap'])))`; the reply side has the `language` dict arm and nothing else, so a Mastodon `Update` carrying `contentMap` and no `language` sets a post's language and leaves a reply's untouched. **Row 4 of the spec's asymmetry table; this entry discharges it.** Not converged for three reasons, any one sufficient: **(a)** the fallback calls `find_language` (`:376-381`), which LOOKS UP and returns `None` for a code the database does not carry, where the reply arm's `find_language_or_create` (`:384-397`) CREATES -- so a copy would first have to choose between two different semantics the file uses within six lines of each other; **(b)** giving replies a language they do not get today is behaviour, not a guard; **(c)** the post copy is itself a live crash (**D255**, `StopIteration` on `"contentMap": {}`), so copying it today would copy the defect into the half that does not have it. **Cost if wrong: replies keep ignoring a language signal posts honour, documented.** **CORRECTION, recorded because it changes what the entry claims: this is not a PAIR asymmetry, it is a THREE-WAY one.** `create_post_reply` (`app/activitypub/util.py:2644-2655`) carries the identical `contentMap` fallback -- `elif 'contentMap' in request_json['object'] and isinstance(request_json['object']['contentMap'], dict): language = find_language(next(iter(request_json['object']['contentMap'])))` -- one block below its own `language` dict arm, exactly the shape this row cites in `update_post_from_activity`. So the fallback exists on TWO of the three code paths that assign a language from an incoming ActivityPub document -- `create_post_reply` and `update_post_from_activity` -- and is absent only from `update_post_reply_from_activity`, the one function this row checked it against. The "pair" framing undercounts by one function; the missing side is unchanged. Verified against source at this commit: all three language blocks -- `create_post_reply` (`:2644-2655`), `update_post_from_activity` (`:3197-3210`), `update_post_reply_from_activity` (`:3024-3031`) -- read side by side. Cost if wrong: a create-path signal this row already tracks for the update path goes unrecorded, documented. | not converged, registered only | reading-level, both blocks and both language helpers read at this commit; pinned as current post-side behaviour by `test_a_post_content_map_supplies_the_language`, `test_a_post_content_map_that_is_not_a_dict_is_ignored` and `test_a_post_language_dict_wins_over_content_map`. The reply side has no `contentMap` test because it has no `contentMap` arm |
| D254 | `update_post_reply_from_activity`'s Mention de-duplication (`app/activitypub/util.py:3077-3110`), against `update_post_from_activity`'s Mention arm (`:3228-3253`) | **Not converged -- the reply path has four suppression rules and the post path has none, so a Mention the reply path would suppress is notified anyway when the same Mention arrives on a post.** The four rules -- the post author (`:3078-3080`), a `post_mention` already sent for this post (`:3082-3088`), a `comment_mention` anywhere in the ancestor chain (`:3090-3101`), and a recipient who authored an ancestor (`:3103-3110`) -- all sit behind `if reply.instance.software == 'mbin' or reply.instance.software in MICROBLOG_APPS:` (`:3077`). The post arm goes straight from `if recipient:` (`:3233`) to the block check and the `Notification`. **Row 9 of the spec's asymmetry table; this entry discharges it. D242 does NOT: D242 is SAME-DOCUMENT duplication** -- one document carrying a Mention tag twice producing two rows, a defect present on **both** sides which none of these four rules addresses either. Not converged because **three of the four rules have no meaning on a post**: a `Post` has no `path`, no ancestors and no comment chain, so this is not a missing copy but an *undesigned rule set*, and deciding what post-side de-duplication even is would be new behaviour. The fourth rule, "the recipient is the post author", would silence a case the post path notifies today. The peer-software gate has no post-side equivalent either. **And the rule set is demonstrably under-specified rather than a settled contract worth mirroring: rule 3 was DEAD CODE until this slice fixed it (D241).** A rule that never fired for the whole of its existence is weak evidence of intent to copy. **Cost if wrong: post Mentions keep bypassing suppressions their reply twins honour, documented.** | not converged, registered only | reading-level, both blocks read side by side at this commit; all four rules pinned on the reply side -- `test_a_microblog_reply_mention_of_the_post_author_is_suppressed` and `test_an_mbin_reply_mention_of_the_post_author_is_suppressed` (rule 1), `test_a_microblog_reply_mention_already_sent_as_a_post_mention_is_suppressed` (rule 2), `test_a_microblog_reply_mention_mirroring_a_comment_mention_in_the_chain_is_suppressed` (rule 3), `test_a_microblog_reply_mention_of_an_ancestor_comments_author_is_suppressed` and `test_a_top_level_microblog_reply_mention_skips_the_ancestor_lookup` (rule 4) -- against `test_a_post_mention_of_a_local_user_notifies_them`, which notifies with no rule applied |

### 5. Five peer-reachable crashes found by the whole-branch review -- D255-D259

All five sit **inside** this slice's scoped region, were missed by twelve tasks
and their reviews, and were found only by the whole-branch review. Each was
re-verified against source by this fix wave before being registered; none is
transcribed from the review's summary. **No production code was changed --
this wave is documentation-only, so none of these could be mutation-proved,
which is the reason of record for every one of them being registered rather
than fixed.**

**New entries rather than marked additions to D245/D246/D247, and the reason
matters.** This file's correction convention (D233, D234, D243) exists for a
later finding that **changes what an existing entry claims** -- the appended,
marked correction preserves the audit trail instead of silently rewriting.
**None of these five changes what any existing entry claims.** Each is a
distinct site raising a distinct exception, and not one of them falsifies a
word of D245, D246 or D247; they *extend the family those entries belong to*,
which is a cross-reference, not a correction. Folding them into three existing
rows would also do two things this register should not: bury five separately
actionable sites inside entries the next slice reads as already triaged, and
change the meaning of rows a reviewer has already signed off on. So: five new
entries, each naming the family member it extends. **D256 is the one that came
closest to being an addition** -- it is a second instance of exactly the
symmetric blind spot D247 documents -- and it stays a separate entry because it
**confirms** D247's claim rather than altering it, which is what a
cross-reference is for.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D255 | `update_post_from_activity`'s `contentMap` fallback (`app/activitypub/util.py:3200-3201`) | **Not fixed -- `next(iter(request_json['object']['contentMap']))` raises `StopIteration` on an EMPTY `contentMap`.** `"contentMap": {}` satisfies both conjuncts of the guard on the line above -- `'contentMap' in request_json['object']` **and** `isinstance(request_json['object']['contentMap'], dict)` (`:3200`) -- and `next(iter({}))` then raises, there being no default argument to fall back on. `update_post_from_activity` is an ordinary function and not a generator, so the `StopIteration` propagates out unconverted; nothing between here and the inbox handler catches it. **Post-only**: the reply function has no `contentMap` arm at all (D253). **Sixth member of the unguarded-peer-input family (D236, D238, D245, D246, D247) and the only one that raises something other than `KeyError` or `TypeError`** -- a reviewer grepping the family for `KeyError` would not find it, which is part of why it survived twelve tasks. A one-line repair exists, `next(iter(…), None)`, and was **not** applied because it is a behaviour choice rather than a guard: it would hand `find_language(None)` a `None` code, whose `Language.query.filter(Language.code == None).first()` returns `None` on a well-formed database and skips the assignment -- i.e. it *decides* that an empty `contentMap` means "no language", which nothing in the file says. **Cost if wrong: a two-character peer document kills every `Update` on a post.** | not fixed, registered only | reading-level plus interpreter-checked at this commit: the guard read at `:3200` and the subscript at `:3201`; `next(iter({}))` confirmed to raise `StopIteration`. Not covered -- `test_a_post_content_map_that_is_not_a_dict_is_ignored` sends a **list**, which fails the `isinstance` conjunct and never reaches the subscript, and `test_a_post_content_map_supplies_the_language` sends a one-key dict; no test in the suite sends an empty one |
| D256 | The `language` dict arm on BOTH sides -- `update_post_from_activity` (`app/activitypub/util.py:3198-3199`) and `update_post_reply_from_activity` (`:3023-3024`) | **Not fixed -- `request_json['object']['language']['identifier']` and `['name']` are read with no membership check on both sides, so `"language": {}` raises `KeyError: 'identifier'` out of either function.** The guard admits any dict whatsoever: `'language' in request_json['object'] and isinstance(request_json['object']['language'], dict)` (`:3197`, `:3022`). A peer sending `"language": {"type": "Language"}` -- a plausible partial, since nothing obliges a peer to carry both keys Lemmy carries -- crashes before either is read. **SYMMETRIC, and that is the point of the entry: the sibling comparison that found every asymmetry defect in this slice structurally CANNOT find it, because both copies are wrong in the same way. That is the blind spot D247 records, and this is its second instance** -- so D247's claim is **confirmed by a second case rather than corrected**, which is why this is a new entry and not an addition to it. Two instances is enough to promote the observation to a method rule: **a mirrored-pair slice needs a non-comparative pass over each half, because its central tool is blind to shared defects by construction.** Not fixed for D247's reason exactly: there is no correct spelling anywhere in the pair to copy, and choosing between "skip the language arm", "fall back to `identifier` alone" and "let it raise" is behaviour. **Cost if wrong: a remotely-triggerable `KeyError` on both halves of the pair, documented.** | not fixed, registered only | reading-level, verified against source at this commit: both guards and both subscript pairs read side by side; `{}['identifier']` confirmed to raise `KeyError`. Not covered -- **every** `language=` payload in `tests/test_ap_update_pair.py` carries both keys (six call sites, all checked), and the non-dict case is pinned on both sides while a dict missing keys is pinned on neither |
| D257 | Both functions' `ap_updated` parse -- `update_post_reply_from_activity` (`app/activitypub/util.py:3058-3061`) and `update_post_from_activity` (`:3264-3267`) | **Not fixed -- the `try` catches `ValueError` only, and a NON-STRING `updated` raises `TypeError`, which escapes.** `datetime.fromisoformat(request_json['object']['updated'])` raises `TypeError: fromisoformat: argument must be str` for `"updated": 123`, `null`, `true`, a list or an object; only a *malformed string* raises the `ValueError` the handler expects. **This one is different in kind from the rest of the family, and that is why it is worth its own row: the existing `except` proves the author intended a bad `updated` to degrade to `utcnow()` rather than lose the whole `Update`.** So this is not an undesigned case -- it is a guard whose domain is one type too narrow, and the intended behaviour is already written down one line below it. Symmetric like D256, and again invisible to the sibling comparison. Not fixed because this wave is documentation-only: `except (ValueError, TypeError):` is arguably mechanical against the author's demonstrated intent, but the campaign's binding rule is that every fix is proved by a mutation failing a NAMED test, and there is no such test. **The next slice should take this first among the five: it is the cheapest -- one token on each of two lines -- and the only one whose intended behaviour the file already states. Cost if wrong: a non-string `updated` loses an entire `Update` on either half of the pair, where a malformed string is handled gracefully.** **CLOSED BY SUB-PROJECT 15, commit `fad7af91`, appended rather than rewritten because it changes this entry's status.** Fixed at **both** sites exactly as this entry specified -- `except ValueError:` widened to `except (ValueError, TypeError):`, one token each, now at `app/activitypub/util.py:3063` (reply, guarding the parse at `:3062`) and `:3269` (post, guarding `:3268`); the line numbers moved from the `:3058-3061` / `:3264-3267` this entry cites because sub-project 15's two create-path fixes shifted the file above them, and **the function attribution is unchanged and re-verified**. Sub-project 15's spec authorised this fix **by name** rather than by its bounded in-unit rule, because both sites lie outside that slice's unit -- which is the mechanism this register wanted when it wrote "the next slice should take this first". **The reviewer's structural check is the part worth keeping**, and it is the question a type-widening fix always raises: both `try` blocks are **single-statement**, so widening the caught type cannot mask an unrelated failure from elsewhere in the same block. That was answered from the code's shape rather than by assertion. **Both clause pointers in this closure note were CORRECTED IN PLACE by sub-project 15's final fix wave, from `:3064`/`:3270` to `:3063`/`:3269`: the cited lines were the `ap_updated = utcnow()` fallback ASSIGNMENTS, one line below the `except (ValueError, TypeError):` clauses this note claims to point at. The parse citations `:3062` and `:3268` were re-verified and are correct. Not appended as a marked correction, and the distinction is the general rule: the append-and-mark convention (D233, D234, D243, D253) protects a cell that recorded what was TRUE at its own verification time and has since been overtaken. A cell this slice wrote with a pointer that was wrong when written is simply wrong, and an append would leave the falsehood standing above its own retraction.** | **FIXED by sub-project 15**, commit `fad7af91` (was: not fixed, registered only) | reading-level plus interpreter-checked at this commit: both `try`/`except` pairs read; `datetime.fromisoformat()` confirmed to raise `TypeError` -- not `ValueError` -- for `123`, `None`, `True`, `['x']` and `{'a': 1}`. **Closure evidence:** `test_a_reply_non_string_updated_falls_back_to_now` and `test_a_post_non_string_updated_falls_back_to_now` in `tests/test_ap_update_pair.py`, each sending an integer `updated` and asserting the **persisted** `ap_updated` fell back to now; both failed pre-fix with `TypeError: fromisoformat: argument must be str`, and each site was mutation-tested **independently** -- reverting one site's `except` clause kills only that site's pin. Test placement was decided by reading both functions rather than by copying: the post-side pin passes `type='Note'` because `update_post_from_activity` reads `request_json['object']['type']` unconditionally further down (**D245**), and the reply-side pin correctly omits it because `update_post_reply_from_activity` never reads `type` |
| D258 | `update_post_from_activity`'s title handling (`app/activitypub/util.py:3183-3188`) | **Not fixed -- `"name": null` raises `AttributeError: 'NoneType' object has no attribute 'upper'`, and the pair guards `content is not None` while guarding nothing on `name`.** `'name' in request_json['object']` (`:3161`) is a membership test that `{"name": null}` satisfies, so `new_title = None` (`:3162`); `old_title != new_title` is then True for any post that has a title, `post.title = new_title` sets the title to `None` **in memory first** (`:3184`), and `new_title.upper()` (`:3185`) raises. **The contrast with D237 is the finding.** Both functions were taught to check `request_json['object']['content'] is not None` (`:3011`, `:3140`) because peers send a null content -- and the null-check was never extended to the sibling field twenty lines below, which peers null for the same reason: a Mastodon status has no `name`, and a peer normalising its `Update` shape emits `"name": null` rather than omitting the key. **Post-only**: the reply function has no title. Not fixed because this wave is documentation-only, and because the repair carries a real choice: appending `and request_json['object']['name'] is not None` sends a null `name` down the `else` branch, which autogenerates a microblog title from the body and sets `post.microblog = True` -- a materially different post from the one the peer described, and a contract question rather than a guard. **Note the in-memory `post.title = None` at `:3184` before the raise.** There is no commit between the two inside this function, so this function persists nothing; whether it becomes visible depends on the caller's session handling, and there are four call sites (`app/activitypub/routes.py:1264`, `:2330`, `:2345`, `app/activitypub/util.py:4317`) plus `app/post/routes.py:2256`. **Cost if wrong: a null `name` loses an `Update`, with a possible nulled title depending on the caller.** | not fixed, registered only | reading-level plus interpreter-checked at this commit: `:3160-3181` read to establish that `new_title` takes the raw value with no coercion on either branch, `:3183-3188` read for the three `.upper()` calls, and the five call sites listed by grep; `None.upper()` confirmed to raise `AttributeError`. Not covered -- every `name` in `tests/test_ap_update_pair.py` is a string, and `test_a_post_with_no_name_autogenerates_a_microblog_title` **omits the key** rather than nulling it, which is the case that does not crash |
| D259 | `update_post_reply_from_activity`'s attachment loop (`app/activitypub/util.py:3045-3054`) | **Not fixed -- a non-string SCALAR *entry* inside an `attachment` LIST raises `TypeError` at `if 'href' in attachment` (`:3047`).** `"attachment": [1]` passes the `isinstance(…, list)` arm (`:3043-3044`), so `attachment_list` is `[1]`, and `'href' in 1` raises `TypeError: argument of type 'int' is not iterable`; the same holds for `null`, a bool and a float. **A STRING entry does not crash** -- `'href' in "https://example/i.png"` is a *substring* test returning `False` (this slice's fact 71), so all three `in` checks miss, `url` stays `''`, and the entry is silently dropped. **D251 is a different case and this entry does not correct it.** D251 is a scalar at the **top level** of `attachment`, which matches neither `isinstance` arm (`:3041-3044`), leaves `attachment_list` at `[]` and never enters the loop; this is a scalar **entry inside a well-formed list**, which does enter it. One is a benign no-op, the other is a crash -- which is exactly why D251 is a coverage gap and this is a register entry of the D236 family. Not fixed because this wave is documentation-only and because the choice -- skip the entry, skip the loop, or accept a bare string entry as a url, which the substring behaviour currently *almost* does by accident -- is behaviour nothing in the file selects. The post function has no attachment handling in the scoped region, so there is still no sibling to vote (D251's reason, unchanged). **Cost if wrong: one wrongly-typed element in an otherwise valid `attachment` list loses the whole `Update`.** | not fixed, registered only | reading-level plus interpreter-checked at this commit: the two `isinstance` arms (`:3041-3044`) and the loop body (`:3045-3054`) read; `'href' in` confirmed to raise `TypeError` for `1`, `None`, `True` and `1.5`, and to return `False` without raising for a string. Not covered -- `test_a_reply_single_attachment_dict_is_appended`, `test_a_reply_attachment_list_is_appended_in_order` and the four other attachment tests supply dict entries only |

**Next free number: D297.** D236-D251 were taken by sub-project 14's twelve
tasks; **D252-D259 were taken by its final fix wave** -- D252-D254 discharging
the three asymmetry-table rows that were pinned but never decided, D255-D259
registering five peer-reachable crashes inside the scoped region that the
review found and the slice did not. That fix wave changed **no production
code**; `git diff -- app/` was empty for it, which is why all eight are
registered and none is fixed. D260-D273 were taken by sub-project 15 --
D260-D269 by its twelve tasks and D270-D273 by its own final fix wave, three
of those four being further copies of **D256**, **D247** and **D255** in this
section's own table -- and its section follows this one. D232 and D219(c) were **not** renumbered -- they
are sub-project 13's entries, closed by this sub-project's Task 11 and updated
in place in sub-project 13's own section. **Two of THIS section's entries have
since been closed the same way and are not renumbered either: D243 by
sub-project 15's commit `3989f55b` and D257 by its `fad7af91`, each carrying an
appended, marked closure note in its own row above.** **D274-D282 were taken by
sub-project 16**, whose subsection 5 indexes the unguarded-peer-input family
this section named -- seventeen entries, **eleven of them registered here**:
D236, D237 and D238 in subsection 1, D245, D246 and D247 in subsection 2, and
D255-D259 in the table immediately above -- without renumbering, moving or
editing any of them. **D283 was then taken by that sub-project's final fix
wave**, which added a third equivalent-mutant entry to the family index's
rejection list and re-derived the partition to D236-D283 -- again editing no
entry of this section.
**D284-D296 were taken by sub-project 17**, which fixed **D284**, the
`vote['name']` half of this section's own family shape, in
`update_post_from_activity`'s two poll loops, and extended the family index to
D236-D296 -- again without renumbering, moving or editing any entry of this
section. **Four of this section's entries are cited by that extension without
being changed**: D236, D237, D238 and D257 as the "fixed" precedent for listing
a repaired member in the index, and **D257 specifically as the precedent
admitting a member whose guard is PRESENT but one type too narrow**, which is
how sub-project 17's D290 and D291 qualify.
(The closing sentence of this note read "If you take D260" until sub-project 16
updated it; that was a stale number left behind when the note's own header was
advanced, not a record of anything that was once true, so it is corrected in
place rather than appended to.)
If you take D297, say so here in the change that takes it.

**Ten shapes worth carrying forward from this sub-project's rulings, now in
`tests/README.md` as facts 71-80 plus a corollary appended to fact 55:** `in`
against a string is a substring test,
so a "wrong type" fixture chosen as a string sails through a downstream
membership conjunct; a test reaching a guard's False side naturally cannot kill
a mutant that forces it False; three ways a later step in the same run masks
what a mutation changed; adding a conjunct can **unkill** an existing test, so
re-run the guard's existing mutations after changing it; the five catalogued
causes of an unkillable clause, now including unreachable data and tautology; a
"missing header" defect must be pinned by asserting the intended **value** on
the **recorded** request, because the HTTP client supplies its own `*/*`; a
quoted code block is the one docstring claim that can be audited mechanically;
the suite produces **shifting** false failures on a stale stack, so any
surprising failure gets `./run_tests.sh --down` and a re-run before it is
believed; **a kill verified in a SIBLING FILE is real coverage, provided the
guard's own test discloses where it lives** (fact 79, the campaign's answer to
the standing question Task 7 raised -- see below); and two ways a mutation round
damages the tree, with the check for each (fact 80). The corollary on fact 55
records the count norm this sub-project exercised six times: **an expected test
count is an estimate, never a reason to ship an unproven conjunct.**

**The standing question Task 7 raised is answered, and the answer is NO: a test
file need not self-contain its kills.** Task 7 found a mutant -- forcing
`link != ''` False, which deletes the `post.url` write -- that nothing in
`tests/test_ap_update_pair.py` kills, and that
`tests/test_unparseable_url_ingress.py::test_an_ordinary_microblog_link_is_still_stored`
does kill. The implementer declined to duplicate that file's ground and
documented the split; the reviewer verified the cross-file kill by reading the
other test and accepted it. **Ruled at the final review: a kill verified in a
sibling file is real coverage, and this campaign has repeatedly forbidden
duplicating another file's ground precisely because duplicate tests rot
independently.** What the reliance requires is **disclosure at the point of
reliance** -- the docstring of the guard's own test must name the file and test
that kills it, so a reader is not misled into thinking the guard is unproven.
Task 7 did exactly that, which is why it was accepted rather than merely
tolerated. **Cost if wrong: a mutant's kill lives one file away from the guard
it proves, findable only by following a docstring pointer.**

**Five false docstrings shipped in this sub-project and were caught by later
tasks** -- "the block breaks out per tag", "matches on url alone", a wrong
function attribution, an overstated crash scope, and a false adjacency claim.
Every one was a **prose** claim about production structure, and not one was
greppable. Exactly **one** quoted code block drifted, and a mechanical `ast`
sweep found it. That ratio is the whole argument for fact 77.

## Sub-project 15: the create path's reply half

`docs/superpowers/specs/2026-09-04-coverage-create-reply-15-design.md` and
`docs/superpowers/plans/2026-09-04-coverage-create-reply-15.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-04-coverage-create-reply-15/`, not committed), on
branch `blentz`. Eleven tasks brought two functions in
`app/activitypub/util.py` under test entire -- `create_post_reply`
(`:2593-2774`) and `notify_about_post_reply` (`:2939-3008`), both spans read
off an **unfiltered** `^def ` scan at this commit -- and fixed **three**
defects in three commits: `acdcf98b` (registered below as **D261**),
`3989f55b` (closing sub-project 14's **D243**) and `fad7af91` (closing
sub-project 14's **D257**). The last two are sub-project 14's entries, which
named this slice as the place to take them; **they are updated in place in
sub-project 14's own section and are not duplicated here** -- read them there,
where each now carries an appended, marked closure note. Tests live in
`tests/test_ap_create_reply.py` (**78 test functions, 80 collected** -- two
parametrized x2 -- after the final fix wave added six; 72 functions / 73
collected before it); D257's two pins live in `tests/test_ap_update_pair.py`,
beside the guard they correct, taking that file from 106 to 108.
`app/activitypub/util.py` measures **71.0362% blended** (72.6356%
statements, 68.1648% branches) after this sub-project, up from 66.8156%; the
floor rises **66 -> 71**. Full suite after this sub-project: **3606 passed, 3
skipped, 6 subtests passed** in 222.45s, against 3531 at the end of
sub-project 14 -- the branch gained **75** tests.

**THE FIRST SUCCESS CRITERION IS MET IN A STRICTLY STRONGER FORM THAN
SUB-PROJECT 14 MET IT, AND THE HONEST STATEMENT OF IT IS NOT "FULL STATEMENT
COVERAGE".** Measured per-region out of the coverage JSON at this slice's last
commit, `notify_about_post_reply` has **zero** uncovered statements and
`create_post_reply` has **two** -- `:2620` and `:2621`, nothing else. Those two
are the body of `if post_id is None:` (`:2619`), which is **dead code**: they
are unreachable by construction, proved against every branch of
`find_reply_parent` (`:1987-2017`) rather than observed, and registered as
**D265**. So: **every reachable statement in the scoped region is covered, and
the two that are not are unreachable by construction.** Claiming the criterion
outright would be false and claiming it failed would be misleading. Sub-project
14 met the criterion with a literal zero; this slice meets it with zero
reachable and **the irreducible remainder proved rather than assumed**, which
is the better outcome of the two and the one worth copying.

**The criterion was met only because it was MEASURED, and the measurement
found a nine-statement gap the plan had no idea it had.** The first coverage
run reported nine uncovered statements in `create_post_reply`, in three places
the plan never enumerated: the parent-*comment* author-block and
`replies_enabled` guards (`:2611-2616`), the inner `post_id` guard
(`:2619-2621`), and the tail handler (`:2769-2771`). The plan's Task 1 brief
said "the five head guards"; source has **seven** plus an outer `else`. The
controller had counted the guards on the *post* and missed the two on the
*parent comment*, and had treated the inner `post_id` guard and the outer
`else` (`:2772-2774`) as one thing -- which Task 1's reviewer flagged at the
time and which was recorded without being acted on. A Task 10c was inserted
before this register task and closed the gap. **The lesson is not "count the
guards more carefully": it is that a criterion stated against a bounded region
and then actually measured catches its own author's arithmetic, and one
stated and assumed does not.**

**AND THE SECOND CRITERION HAD A BLIND SPOT NO MEASUREMENT COULD HAVE FOUND:
coverage.py emits NO ARC for a conditional expression.** `a if c else b` is one
statement on one line, so an unexercised arm is neither a missed statement nor
a partial branch -- **the 71.0362% blended figure above is silent about every
ternary in the scoped region**, and criterion 2 (no guard survives a dropped
conjunct) is therefore not measurable by coverage for ternaries **at all**.
The whole-branch review found **six** unexercised ternary arms in the scoped
region, every one reachable with a single fixture line, at a point where both
coverage numbers said the region was done: `saved_json = request_json if
store_ap_json else None` (`:2595`, the `else`), `language_id = language.id if
language else None` (`:2652`, the `else`), `author.ap_id if author.ap_id else
author.user_name` at three sites (`:2755`, `:2952`, `:2994`, the `else` at
each), and `community.ap_id if community.ap_id else community.name` (`:2951`,
the **`if`**, because `make_community` never sets `ap_id`). The final fix wave
pinned all six, each mutation-proved to kill exactly its own test. **The
`language` one was a real guard, not a display-name fallback**: `find_language`
returns `None` on a miss, so a mutant dropping `if language` raises
`AttributeError` out of `create_post_reply`, and it survived the entire module.
**The operational rule, now `tests/README.md` fact 87: enumerate conditional
expressions by READING the region, because the coverage report will never list
one -- a region at 100% statements and 100% branches can still have an
unexercised ternary arm behind every one of them.**

**The fix rule is unchanged from sub-projects 13 and 14 and produced a
different answer here than the spec predicted: a defect is fixed under a
bounded authorisation when the correct spelling already exists in the file and
the change is mechanical; it is registered when the fix would require choosing
new behaviour for a case the codebase has never handled.** The spec listed
three mirrored crash paths for fixing and expected three fixes. Two were
mechanical copies of the twin sub-project 14 had just repaired (D261, D243).
**The third -- the unflushed `Language.id` -- was declined, and that decline is
the most instructive thing in this slice**, because the twin's repair is
*structurally unavailable* rather than merely awkward. It is registered as
**D260** and subsection 1 is given over to it.

**Sub-project 14's grep-for-the-twin corollary needed a corollary of its
own, and this slice supplied it.** SP14 ruled: before registering a defect for
want of a correct spelling, grep the file for the twin. Here the create path's
Mention loop has **no** `existing_notification` check where its twin has one
(**D262**) -- so the grep succeeds, and copying is still wrong, because **the
twin's check is itself a registered defect (D242)**. Copying it would import a
guard that does not dedupe within one document, which is the very case D262 is
about, and would let the slice claim a fix that fixes nothing. **The twin must
also work: check the register for whether the sibling you are about to copy is
itself a D-entry.** That is now fact 83 in `tests/README.md`.

**Every row of the spec's asymmetry table is discharged, and five of the eleven
needed no new entry.** Rows 1-3 (null `content`, the unflushed `Language.id`,
the empty-tuple `IN ()`) are D261, D260 and D243. Row 4 (rule 3's `continue`
placement) **needs no entry: the asymmetry no longer exists** -- sub-project
14's Fix F re-indented the update copy to match this one, and Task 6
re-confirmed the create copy is the working original by mutation (its M16
proves the spelling load-bearing rather than incidental). Row 5 (`contentMap`)
was discharged before this task, by the appended three-way correction to
**D253** at commit `ea48c6cb`; note that the spec's sixth success criterion
named **D250** as the entry to correct and **was wrong** -- D250 is about *when*
a resolved language is applied and never mentions `contentMap`, and D253's own
prose already said "and D250 does NOT cover this". The Task 10 implementer
checked both entries rather than obeying the brief, and redirected. Row 10
(`source['mediaType']`) already agrees on both sides and is the one row that
never needed anything. Rows 6-9 and 11 are registered as **D266-D269** and
**D263**.

**Three registrations in this slice are about the same structural fact: this
function converts failures into silence.** The tail `except Exception as ex`
(`:2769-2771`) catches everything the `try` at `:2700` covers, logs `str(ex)`
through a `log_incoming_ap` that writes nothing unless
`LOG_ACTIVITYPUB_TO_DB` is on, and returns `None` -- indistinguishable from
every head guard's refusal (**D263**). D243's crash lived inside it for the
whole of its existence, which is why the create-path copy went unnoticed a
slice longer than the update-path copy. And it is why **four negative tests in
this file had to be routed around D243 individually** while five did not: an
unfixed crash inside a broad handler is a downstream route to every "nothing
happened" assertion in the function, and blanket routing would have hidden
which tests actually needed it. That is now fact 81.

### 1. The fix this slice declined, and why -- D260

The spec authorised this fix by name and expected it to land. It did not, and
the reason is a limit on the campaign's fix rule worth more than the fix would
have been: **the correct spelling exists in the file and is still unavailable
here, because the two sites have different shapes.** Every alternative was
enumerated and each was shown to change behaviour rather than to copy a
sibling. The decline was stress-tested at review, at a fix round, and at a
re-review; it survived all three and is better argued now than when it was
made.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D260 | `create_post_reply`'s language arm (`app/activitypub/util.py:2646-2649`), plus two identical reads outside this slice: `refresh_instance_chooser` (`app/shared/tasks/maintenance.py:1033`) and `retrieve_mods_and_backfill` (`app/community/util.py:259-263`) -- function attributions derived from an `ast` walk, separately from the line ranges | **Not fixed -- `find_language_or_create` returns a row it has only `add()`ed, so `.id` is `None`, and a reply carrying a language code this instance has never seen is created with `language_id` NULL.** `find_language_or_create` (`:384-397`) does `db.session.add(new_language)` and returns without flushing (`:396-397`), the app factory sets `session_options={"autoflush": False}` (`app/__init__.py:81`), and the caller reads `language_id = language.id` (`:2649`). **Same root cause and same symptom half as D239(a)**, which sub-project 14 fixed in both update functions by assigning through the relationship -- `reply.language = language` (`:3031`), with the reason quoted in a comment at `:3028-3030`. **That shape is STRUCTURALLY UNAVAILABLE here, which is the whole entry.** `create_post_reply` has no `PostReply` instance to assign a relationship to: it must produce an `int` for `PostReply.new(..., language_id=language_id, ...)` (`:2701-2703`), and `PostReply.new` (`app/models.py:2967-2968`) forwards it straight into the constructor as `language_id=language_id` (`app/models.py:3001`). The instance the relationship would be assigned on is created *inside* the callee. **The strongest alternative was named by the reviewer rather than by the implementer, and it is a spelling that exists in this very function**: `db.session.commit()` before reading `.id` -- the flair arm commits at `:2699`, twenty lines above. It still fails the rule. `PostReply.new` raises `PostReplyValidationError` from **seven** places (`app/models.py:2978`, `:2981`, `:3020`, `:3031`, `:3034`, `:3041`, `:3044`) before its own `session.add`/`session.commit` (`:3047-3048`); today the pending `Language` **stays pending** and any later commit in the request flushes it, and an early commit would make that write **unconditional and immediate** -- a `Language` row persisted for replies the function then refuses. That is new behaviour, not a copy. The remaining candidates are worse: `db.session.flush()` appears **zero** times in `app/activitypub/util.py`; widening `PostReply.new`'s signature touches five other callers; flushing inside the shared `find_language_or_create` changes a house pattern all four `*_or_create` helpers follow. **All three sites take the SAME `db.session.add` branch -- none passes `session=` to `find_language_or_create`**, and this is recorded explicitly because a task report claimed the `app/community/util.py` site passes `session=session` and it does not: the call at `:261-262` has exactly two positional arguments, and the `session=session` belongs to the `PostReply.new` call **twelve lines later** at `:272-273`. Three identical sites is a stronger finding than two-plus-an-analogue, but the reasoning that got there was wrong and must not be repeated. **Cost if wrong: a reply carrying a language code new to this instance is created with no language, silently, at three sites; the `Language` row is created and the reply does not point at it.** | not fixed, registered only | measured, not read: `test_an_unseeded_language_is_created_but_not_applied` pins it as **current behaviour, explicitly not endorsed** -- the `Language` row is created and gets an id, and the reply's `language_id` is `None`. The other two sites are reading-level, verified against source at this commit |

### 2. One defect fixed here, test-first with a mutation-proved test -- D261

Two of sub-project 14's entries were also closed by this slice and are **not**
duplicated as new numbers: **D243** (the empty-tuple `IN ()`, commit
`3989f55b`) and **D257** (the non-string `updated`, commit `fad7af91`). Both
are updated in place in sub-project 14's section, each with an appended,
marked closure note, per this file's correction convention.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D261 | `create_post_reply`'s content gate (`app/activitypub/util.py:2633`), against `update_post_reply_from_activity`'s (`:3014`) and `update_post_from_activity`'s (`:3143`) | **FIXED, commit `acdcf98b`. The create path gated on `'content' in request_json['object']` alone, so a peer sending `"content": null` -- which Mastodon and Lemmy both do on some edits -- raised `AttributeError: 'NoneType' object has no attribute 'startswith'` at `:2634`.** This is D237's defect in a third copy: sub-project 14 fixed the reply-update half from the post-update half, and the create half was left carrying it. The fix appends ` and request_json['object']['content'] is not None`, the sibling's spelling character for character; a null content then leaves `body`/`body_html` at the `''` they were initialised with at `:2632` and the rest of the document is still honoured. **The crash is raised ABOVE the function's tail `try` (`:2700`), and that distinction is the finding inside the finding**: unlike D243's crash, which the tail `except Exception` swallowed into a `None` return (D263), this one **propagated out to the caller** and the reply was never created -- so the two mirrored crashes in the same function had opposite failure modes, one loud and one silent, and only the loud one was ever going to be noticed in production. **A second test was required by the fix's own mutation run, not by the brief**: dropping the pre-existing `'content' in ...` conjunct survived every other test in the module, because every other document in the file carries `content`. That is fact 68's "one mutation per clause" arriving as a live survivor rather than as a rule. | **fixed**, commit `acdcf98b` | measured: `test_a_reply_with_null_content_is_created_with_an_empty_body`, written as a pin on the `AttributeError` and inverted in the same commit to assert the persisted `PostReply` row's empty `body`; plus `test_a_reply_missing_the_content_key_is_created_with_an_empty_body`, added by the fix's mutation run to kill the membership conjunct |

### 3. Four items registered, not fixed -- D262-D265

Each carries the reason it was not fixed. **None of these four is a new
instance of an existing entry's claim, so none is folded in as an appended
correction** -- D262 is a distinct site from D242's two (which are both in the
update pair), D264 extends the D251/D259 attachment family without falsifying
either, and D263 and D265 have no precedent at all.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D262 | `create_post_reply`'s Mention notification block (`app/activitypub/util.py:2748-2766`), against `update_post_reply_from_activity`'s (`:3115-3134`) | **Not fixed -- the create path has NO de-duplication check at all, so a document carrying the same `Mention` tag twice for one recipient produces TWO notification rows and a double-incremented unread counter, unconditionally.** The create path goes straight from `blocked_senders = blocked_users(recipient.id)` / `if post_reply.user_id not in blocked_senders:` (`:2748-2749`) to `author = User.query.get(...)` and the `Notification` construction (`:2750-2766`). The twin interposes `existing_notification = Notification.query.filter(Notification.user_id == recipient.id, Notification.url == f"...{reply.id}").first()` / `if not existing_notification:` (`:3117-3119`) at exactly that point. **This is NOT D242. D242 is the update pair's check being DEFEATED by `autoflush=False`; here there is no check to defeat**, and D242's own scope is the two update functions, so this entry does not correct it. Not fixed, and the reason is a **limit on this campaign's own fix rule**: the rule says fix when the correct spelling already exists in the file, and it does -- but **that spelling is itself registered as defective**. D242 records that the first tag's pending `db.session.add` is invisible to the second tag's query under `session_options={"autoflush": False}` (`app/__init__.py:81`), so the twin's check dedupes **across** calls (a redelivered document) and **not within** one. Copying it would import a known-broken guard and let this slice claim a fix that fixes nothing. A real repair means *choosing* a mechanism -- a flush after the `add`, an in-memory seen-set across the loop, or de-duplicating `local_users_to_notify` (`:2681`, `:2689`) before it -- which is new behaviour for a case the codebase has never handled. **Cost if wrong: a peer can double a local user's mention notifications by repeating a tag, at a third site, documented and unfixed.** | not fixed, registered only | reading-level, verified against source at this commit by reading both blocks side by side; the sixth item of Task 5's brief was **declined rather than faked** -- the no-duplicate case could not be pinned on the ungated path without either provoking the forbidden tail handler or pinning this defect, and declining beat pinning the wrong thing |
| D263 | `create_post_reply`'s tail handler (`app/activitypub/util.py:2769-2771`), against `update_post_reply_from_activity`, which has none | **Not fixed -- a bare `except Exception as ex: log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, str(ex)); return None` wraps everything from `PostReply.new` through the whole notify loop (`:2700-2768`), converting real failures into a return value indistinguishable from every head guard's refusal.** Three separate things it demonstrably swallows, each confirmed rather than supposed: **(a)** all seven `PostReplyValidationError`s `PostReply.new` raises (`app/models.py:2978`, `:2981`, `:3020`, `:3031`, `:3034`, `:3041`, `:3044`), including `'Duplicate reply'` and `'Replier blocked'` -- and `'Replier blocked'` produces a log message so close to the head guard's `'Post author blocked replier'` (`:2629`) that a **substring** assertion on the log row passes on the wrong guard, which is how Task 1 found it; **(b)** D243's `ProgrammingError` for the whole of that defect's life, which is why the create-path copy of D240 went undetected one slice longer than the update-path copy, and which meant creating a top-level microblog comment that Mentioned a local user silently produced **no notification while `PostReply.new` had already committed the reply row**; **(c)** any database error raised inside the `try`, whereupon the handler's own `log_incoming_ap` runs `db.session.add` and `db.session.commit` (`:4583-4584`) **inside the already-aborted transaction** and substitutes a second failure for the first. Not fixed because narrowing it is a behaviour choice nothing in the file makes: `PostReply.new`'s validation errors are arguably *meant* to be caught here (they are this function's normal refusal path), while a `ProgrammingError` is not, and no sibling votes -- the twin has no tail handler whatsoever, which is the asymmetry, not a model. **Row 11 of the spec's asymmetry table; this entry discharges it. Cost if wrong: every failure in the create path's second half looks like a policy refusal, and the log row that would distinguish them is not written unless `LOG_ACTIVITYPUB_TO_DB` is on.** | not fixed, registered only | measured: `test_the_tail_exception_handler_pins_a_postreply_new_validation_error` reaches it through a **real** `PostReplyValidationError('Comments are disabled on this post')` rather than an injected exception, and asserts the **exact** log message; (a) and (c) verified against source at this commit, (c) also by Task 6's declined log assertion, which was declined for precisely this reason |
| D264 | `create_post_reply`'s attachment loop (`app/activitypub/util.py:2666-2677`), the create-path twin of **D259** | **Not fixed -- a non-string SCALAR *entry* inside an `attachment` LIST raises `TypeError` at `if 'href' in attachment` (`:2668`).** `"attachment": [1]` passes the `isinstance(..., list)` arm (`:2664-2665`), so `attachment_list` is `[1]`, and `'href' in 1` raises `TypeError: argument of type 'int' is not a container or iterable`; the same holds for `null`, a bool and a float. A **string** entry does not crash -- `'href' in "https://example/i.png"` is a substring test returning `False` (fact 71), so all three `in` checks miss, `url` stays `''` and the entry is silently dropped. **The crash is raised ABOVE the tail `try` (`:2700`), so unlike D243 it propagates out** -- the same distinction D261 records, reaching a third site. **D251's case is separately confirmed as a benign no-op here, identical to the twin's, and is deliberately NOT registered**: a scalar at the *top level* of `attachment` matches neither `isinstance` arm (`:2662-2665`), leaves `attachment_list` at `[]`, and the loop and the `if attachment_list:` regeneration gate (`:2676-2677`) both no-op. That answer is worth recording because the interesting alternative was a crash. **This entry does not correct D251 or D259**: D251's and D259's claim that "the post function has no attachment handling in the scoped region, so there is no sibling to vote" remains true of the post function, and this third copy behaves identically to D259's, so it extends the family rather than falsifying either. Not fixed for D259's reason unchanged -- the choice between skipping the entry, skipping the loop, and accepting a bare string entry as a url (which the substring behaviour currently *almost* does by accident) is behaviour nothing in the file selects. **Cost if wrong: one wrongly-typed element in an otherwise valid `attachment` list loses the whole `Create`, at a second site.** | not fixed, registered only | reading-level plus interpreter-checked at this commit: the two `isinstance` arms (`:2662-2665`) and the loop body (`:2666-2677`) read; `'href' in` confirmed to raise `TypeError` for `1`, `None`, `True` and `1.5` and to return `False` for a string. Found by the Task 3 **reviewer**, and only because it queried the register for what was already known about this loop rather than answering the brief's narrower question about D251 -- **brief the register, not just the case**. Not covered: all eight attachment tests supply dict entries |
| D265 | `create_post_reply`'s inner parent-post guard (`app/activitypub/util.py:2619-2621`) | **Not a defect and not fixable -- `if post_id is None:` inside `if post_id or parent_comment_id or root_id:` is a TAUTOLOGY, and its two-statement body is DEAD CODE that no honest test can ever cover.** Registered because it is the entire residual of this slice's first success criterion and must be on the record as *proved* rather than *assumed*. The proof is against every branch of `find_reply_parent` (`:1987-2017`), not against a sample: both places that set `parent_comment_id` (`:1995-1997` and `:2009-2011`) set `post_id = parent_comment.post_id` in the same statement group, and `root_id` is set **only** alongside `parent_comment_id`, in those same two places; the two places that set `post_id` alone (`:2003`, `:2015`) set it from a resolved `Post.id`. With the outer gate at `:2607` requiring at least one of the three to be truthy, `post_id` is non-`None` on every path that enters the block, so the inner guard's True branch is unreachable. **Catalogued cause 4 -- tautology (`tests/README.md` fact 75) -- in a new shape**: not a guard whose body writes what its own False condition already asserts, but a guard whose condition is falsified by a caller's invariant. (This entry originally cited "cause 5"; corrected in place by sub-project 15's final fix wave against fact 75's own numbering, where 4 is tautology and 5 is unreachable data. The substantive claim is unchanged, and fact 75's cause-4 wording was widened in the same change to admit this second shape.) Not fixed because there is nothing to fix: deleting the guard would be a cleanup with no behavioural effect and no test could prove it either way. Documented as a source-adjacent comment in `tests/test_ap_create_reply.py` rather than pinned with a fabricated fixture. **Note the guard it is NOT**: the outer `else:` at `:2772-2774` logs `'Unable to find parent post/comment'` and is reached when `find_reply_parent` returns nothing at all; that one is live, covered, and was treated as the same guard as this one by the plan. Both messages contain "parent post", which is how a substring assertion passed on the wrong row in Task 1. **Cost if wrong: nothing today -- this is the criterion's residual, recorded so nobody re-derives it or writes a fake fixture for it.** | not a defect, registered only | proved from source at this commit and independently re-derived at review against every branch of `find_reply_parent`; measured out of the coverage JSON as the only two uncovered statements in either scoped function |

### 4. Four asymmetry-table rows discharged by registration -- D266-D269

The spec's sixth success criterion requires every row of its asymmetry table to
be **either** fixed with a mutation-proved test **or** registered with a stated
reason. Rows 1-3 were fixed (D261, D260's decline, D243); rows 4 and 10 need
nothing (row 4's asymmetry was resolved by sub-project 14's own Fix F, row 10
already agrees); row 5 was discharged by the appended correction to D253; row
11 is D263. **These four close rows 6, 7, 8 and 9.** Sub-project 14 learned the
hard way that **a row covered by a test is not a row discharged** -- all four
of these are covered, which is exactly why they could have been missed.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D266 | `create_post_reply`'s no-language fallback (`app/activitypub/util.py:2653-2655`), against `update_post_reply_from_activity`'s language block (`:3025-3031`) | **Not converged -- only the create path falls back to `site_language_id()` when a document carries neither `language` nor `contentMap`.** The create path's `else:` imports `site_language_id` from `app.utils` and assigns it (`:2654-2655`); the twin's language block is a single `if` with no `elif` and no `else`, so an `Update` carrying no language signal leaves the reply's existing language **untouched**. A peer that creates a reply with no language and then edits it gets the instance default on create and no change on update, which is coherent; a peer whose *create* omitted the language deliberately gets it overridden anyway. **Row 6 of the spec's asymmetry table; this entry discharges it.** Not converged because whether a reply's language should default to the instance's is a **decision, not a guard** -- and the two halves are not even answering the same question: create must supply *some* value for a NOT NULL-shaped write, update need not. Nothing in the file votes, and the post-side create path (`app/models.py:2133-2135`) agrees with *this* half, which makes the update pair the odd one out rather than this function. **Cost if wrong: nothing today -- this is a signpost for whoever converges the create and update paths.** | not converged, registered only | reading-level, all three language blocks read side by side at this commit; the create-path arm is pinned by `test_neither_language_nor_content_map_falls_to_site_language_id` and `test_a_non_dict_content_map_is_ignored_in_favour_of_site_language_id` |
| D267 | `create_post_reply`'s two-phase Mention handling (`app/activitypub/util.py:2679-2689` collecting, `:2704-2766` notifying), against `update_post_reply_from_activity`'s inline notification | **Not converged -- the create path collects `local_users_to_notify` in one loop and notifies in a second, and the twin notifies inside the tag loop.** The create path scans `request_json['object']['tag']` and appends lowered `profile_id`s to a list (`:2681-2689`), then iterates that list *after* `PostReply.new` has returned (`:2704`), resolving each to a `User` and applying the four suppression rules. The twin does all of it inline. **Row 7 of the spec's asymmetry table; this entry discharges it.** **The shape is not cosmetic and this entry exists to say so: it is WHY this copy's suppression rule 3 works and the update copy's did not.** The create path's rule 3 (`:2722-2733`) can put its ancestor-chain query and its `continue` at the same indent as rules 1, 2 and 4 because the enclosing loop is over *recipients*; the update copy, iterating tags, had the query nested one level too deep and the `continue` advanced a loop that was about to advance anyway -- dead code for the whole of its existence, fixed as **D241** by re-indenting it to match *this* function. Not converged because restructuring the update path's tag loop into two phases is a rewrite, not a guard, and because after D241 the two now produce the same suppressions -- the asymmetry is in the shape, not in the behaviour. **Cost if wrong: nothing today; recorded because the next person to "simplify" the create path's two phases would re-create D241.** | not converged, registered only | reading-level, both blocks read side by side at this commit; the create path's four rules are each pinned -- `test_a_microblog_mention_of_the_post_author_is_suppressed` (rule 1), `test_a_microblog_mention_mirroring_a_post_mention_is_suppressed` (rule 2), `test_a_microblog_mention_mirroring_a_comment_mention_is_suppressed` (rule 3), `test_a_microblog_mention_of_an_earlier_commenter_in_the_chain_is_suppressed` (rule 4) -- each traced against every other rule, the blocked-senders check and an unresolved recipient to prove it fires alone |
| D268 | `create_post_reply`'s `distinguished` read (`app/activitypub/util.py:2657`), against `update_post_reply_from_activity`'s (`:3034-3035`) | **Not converged -- an ABSENT `distinguished` key sets the value to `False` on create and leaves it UNCHANGED on update.** The create path reads `distinguished = request_json['object']['distinguished'] if 'distinguished' in request_json['object'] else False` and passes it to `PostReply.new` (`:2702`); the twin reads `if 'distinguished' in request_json['object']: reply.distinguished = request_json['object']['distinguished']` with no `else`. So a moderator's distinguished reply, edited by a peer whose `Update` omits the key, stays distinguished -- correct -- while the same omission on create silently un-distinguishes nothing, because there is nothing to un-distinguish. **The asymmetry is real but currently harmless, and saying which is the honest disposition.** **Row 8 of the spec's asymmetry table; this entry discharges it.** Not converged because the create path *must* supply a value to a positional parameter of `PostReply.new` (`app/models.py:2967-2968`) and the update path must not clobber an existing one -- the two shapes follow from the two call sites, and making them identical would require either a sentinel or a model default, which is a design change. **Cost if wrong: nothing today -- a signpost, not a live defect.** | not converged, registered only | reading-level, both reads at this commit; the create-path ternary's default is exercised by every test in the file that omits the key, and the flair and language tests assert the persisted row rather than the passed argument |
| D269 | `update_post_reply_from_activity`'s `repliesEnabled` read (`app/activitypub/util.py:3037-3038`), absent from `create_post_reply` | **Not converged -- a peer can turn replies off on an existing comment and cannot create one with replies already off.** The twin reads `if 'repliesEnabled' in request_json['object']: reply.replies_enabled = request_json['object']['repliesEnabled']`; the create path never reads the key, so a `Create` carrying `"repliesEnabled": false` produces a reply with the column's default and the peer's stated intent is dropped. **Row 9 of the spec's asymmetry table; this entry discharges it.** **Do not confuse this with the guard at `:2614`**, and the two-claim citation rule is why this sentence exists: `create_post_reply` *does* read `parent_comment.replies_enabled` (`:2614-2616`), but that is the **parent's** column being used as an admission guard on the incoming reply, not the incoming document's `repliesEnabled` key being applied to the new row. Same identifier, different subject, different direction. Not converged because adding the read means widening `PostReply.new`'s signature or a post-construction assignment plus a commit this function does not otherwise make, and because honouring a peer's `repliesEnabled` on creation is a federation-contract decision -- the field is serialised outbound at `:255` and nothing says inbound must be symmetric. **Cost if wrong: a peer's replies-disabled intent is honoured on edit and dropped on create, documented.** | not converged, registered only | reading-level, both sites and the outbound serialisation at `:255` read at this commit; the parent-comment guard at `:2614-2616` is separately pinned by `test_a_reply_to_a_locked_comment_is_refused`, whose docstring distinguishes the two |

### 5. Four unregistered create-path crashes -- D270-D273

Added by the **final fix wave**, after the whole-branch review found four
unguarded subscripts in `create_post_reply` that twelve tasks had read past.
All four raise from a point **above** the tail `try` (`app/activitypub/util.py:2700`),
so unlike D243 they propagate out of the function instead of being swallowed
into a `None` return -- the distinction D261 and D264 already record, now at
four more sites.

**Three of the four are additional copies of entries that scoped themselves to
other functions, and this slice had already registered one such copy (D264, as
D259's third) before failing to register these.** That inconsistency is the
finding worth carrying: the copy-hunt was run for the entry the brief named and
not for the family. The mechanical remedy is fact 82's, one step wider --
**when an entry scopes itself to a set of functions, grep the SUBSCRIPT it
names across every function in the campaign's scope, not the entry's.**

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D270 | `create_post_reply`'s `language` dict arm (`app/activitypub/util.py:2646-2648`), the **THIRD copy of D256**, which scopes itself to the two update functions | **Not fixed -- `request_json['object']['language']['identifier']` and `['name']` are read with no membership check, so `"language": {}` raises `KeyError: 'identifier'` out of `create_post_reply`.** The guard at `:2646` admits any dict whatsoever -- `'language' in request_json['object'] and isinstance(request_json['object']['language'], dict)` -- and a peer sending `"language": {"type": "Language"}`, a plausible partial, crashes before either key is read. D256 registers the identical code on both update halves and states its scope as "BOTH sides" of that pair; the create path is a third side it did not look at. (D256 cites those two sites as `:3197-3199` and `:3022-3024`; at this commit they read `:3200-3202` and `:3025-3027` -- a uniform three-line shift from sub-project 15's create-path fixes. **D256's numbers are left as written**: they recorded what was true at that cell's own verification time, which is precisely the case the append-and-mark convention protects, and re-verification confirms the function attribution is unchanged.) **This does not correct D256 and does not weaken its method rule** -- its point was that a sibling comparison is blind to a defect shared by both siblings, and a third identical copy outside the pair strengthens that: the comparison is blind to shared defects *and* to everything outside the compared set. Not fixed for D256's reason unchanged: there is no correct spelling anywhere in the three copies to copy, and choosing between "skip the language arm", "fall back to `identifier` alone" and "let it raise" is behaviour. **Cost if wrong: a remotely-triggerable `KeyError` on a `Create`, at the one site of three that is reachable without an existing row to update.** | not fixed, registered only | reading-level plus interpreter-checked at this commit: the guard at `:2646` and the subscript pair at `:2647-2648` read; `{}['identifier']` confirmed to raise `KeyError: 'identifier'`; the tail `try` located at `:2700`, below the raise. Not covered -- all THREE dict-valued `language=` payloads in `tests/test_ap_create_reply.py` (`test_a_language_dict_is_applied`, `test_an_unseeded_language_is_created_but_not_applied`, `test_a_language_dict_wins_over_a_present_content_map`) carry both keys, and the one non-dict payload (`test_a_non_dict_language_is_ignored_in_favour_of_content_map`) fails the `isinstance` conjunct and never reaches the subscript; no test sends a dict missing a key |
| D271 | `create_post_reply`'s Markdown `source` arm (`app/activitypub/util.py:2637-2639`), the **THIRD copy of D247**, which scopes itself to the two update functions | **Not fixed -- `body = request_json['object']['source']['content']` is read with no `'content' in` check, so a `source` dict carrying `"mediaType": "text/markdown"` and no `content` raises `KeyError: 'content'` out of `create_post_reply`.** The four-conjunct guard above it (`:2637-2638`) checks `'source' in`, `isinstance(..., dict)`, `'mediaType' in` and the mediaType's value -- every key the arm reads except the one it actually consumes. D247 registers the identical read on both update halves and calls it symmetric across that pair; it is symmetric across all three. (D247 cites those two sites as `:3145` post and `:3017` reply; at this commit the two reads are at `:3148` and `:3020`, the same three-line shift, and **D247's numbers are left as written** for the reason given in D270.) Not fixed for D247's reason unchanged: all three copies read it the same way, and choosing between "skip the Markdown arm" and "fall through to the HTML body" is behaviour. **Cost if wrong: one more remotely-triggerable `KeyError`, now on the create path as well as both update halves, documented.** | not fixed, registered only | reading-level plus interpreter-checked at this commit: the guard at `:2637-2638` and the subscript at `:2639` read; `{'mediaType': 'text/markdown'}['content']` confirmed to raise `KeyError: 'content'`; the tail `try` located at `:2700`, below the raise. Not covered -- the four `source=` tests in `tests/test_ap_create_reply.py` supply either a non-dict, a dict with no `mediaType`, a non-markdown `mediaType`, or a complete markdown source; none sends a markdown `source` missing `content` |
| D272 | `create_post_reply`'s `contentMap` fallback (`app/activitypub/util.py:2650-2651`), the **SECOND copy of D255**, which scopes itself "post-only" | **Not fixed -- `next(iter(request_json['object']['contentMap']))` raises `StopIteration` on an EMPTY `contentMap`.** `"contentMap": {}` satisfies both conjuncts of the guard on the line above (`:2650`) -- `'contentMap' in request_json['object']` **and** `isinstance(..., dict)` -- and `next(iter({}))` then raises, there being no default argument. `create_post_reply` is an ordinary function, not a generator, so the `StopIteration` propagates out unconverted. **The join this slice failed to make, and it is the reason this entry exists rather than an appended note.** D253's own **CORRECTION**, written by sub-project 15, quotes this exact line -- `language = find_language(next(iter(request_json['object']['contentMap'])))` at `:2644-2655` -- to prove the `contentMap` asymmetry is three-way rather than two-way; D253's body, five sentences earlier, calls the post copy "itself a live crash (**D255**, `StopIteration` on `"contentMap": {}`)". The slice wrote both sentences, about the same line, in the same cell, and did not notice that the copy it had just found carries the crash the cell had just named. **D255's "post-only" claim is CORRECTED by this entry as to the crash's scope and CONFIRMED as to its mechanism**: the reply *update* function still has no `contentMap` arm (D253), so "post-only" was true of the pair D255 was comparing and false of the file. (D255 cites the post site as `:3200-3201`; at this commit it reads `:3203-3204`, the same three-line shift, and **D255's numbers are left as written** for the reason given in D270.) Not fixed for D255's reason unchanged: `next(iter(…), None)` would hand `find_language(None)` a `None` code and thereby *decide* that an empty `contentMap` means "no language", which nothing in the file says -- and here that decision is visible, because `language_id = language.id if language else None` (`:2652`) would quietly persist a null language rather than raise. **Cost if wrong: a two-character peer document kills every reply `Create`, not only every post `Update`.** | not fixed, registered only | reading-level plus interpreter-checked at this commit: the guard at `:2650` and the subscript at `:2651` read; `next(iter({}))` confirmed to raise `StopIteration`; the tail `try` located at `:2700`, below the raise. Not covered -- `test_a_non_dict_content_map_is_ignored_in_favour_of_site_language_id` sends a **string**, which fails the `isinstance` conjunct, and all four dict-carrying `contentMap` tests send exactly one key; no test in the module sends an empty dict. The `else None` arm at `:2652` is now pinned by `test_a_content_map_naming_an_unknown_language_leaves_the_reply_unlanguaged`, which reaches `find_language` returning None by a MISS rather than by an empty map, and so does not reach this crash |
| D273 | `create_post_reply`'s flair block (`app/activitypub/util.py:2691-2699`) -- **no precedent entry, and no sibling either: neither update function writes a `UserFlair`.** (`update_post_from_activity` does carry a block spelled "flair", `:3257-3264`, but it clears and rebuilds `post.flair` -- community post-flair tags resolved through `find_flair_or_create` -- which is a different entity from the per-user, per-community `UserFlair` row this block writes. Same word, different table; the two must not be compared.) | **Not fixed -- `request_json['object']['flair'].strip()` (`:2698`) raises `AttributeError` for a TRUTHY NON-STRING flair, and the sibling branch nine lines up assigns the same value with neither `.strip()` nor a type check.** The block's guard is `'flair' in request_json['object'] and request_json['object']['flair']` (`:2691`) -- membership plus truthiness, no type check -- so `"flair": 123`, `"flair": true` and `"flair": ["gold"]` all enter it. Which of the two branches runs is decided by whether a `UserFlair` row already exists for this user in this community (`:2692-2694`): the **create** branch strips (`:2698`) and the **update** branch assigns raw (`:2695`). Two consequences, and the second is the more interesting: **(1)** a truthy non-string crashes the whole `Create` when the user has no flair here yet and does not crash on `.strip()` when they do; **(2)** for an ordinary padded STRING, `"flair": "  gold  "` is stored as `gold` on first arrival and as `  gold  ` on every subsequent one -- **an asymmetry between two branches of a single `if`/`else`, which is a narrower thing than every other asymmetry in this register and is exactly why the pair-comparison method could not see it.** It has been recorded since Task 7, but only in the docstrings of `test_a_flair_on_a_user_with_none_creates_a_user_flair_row` and `test_a_flair_on_a_user_with_an_existing_flair_updates_it`, where each names the other's spelling to keep the two fixtures distinguishable -- a true observation, correctly attributed, with no register entry behind it. Not fixed because the two candidate repairs choose behaviour: stripping in both branches changes what a re-sent padded flair persists, and type-guarding the block decides whether a non-string flair is dropped or fails the whole `Create`, neither of which the file states. **Cost if wrong: a non-string flair loses an entire reply `Create` for first-time flair-setters only, and a padded flair means two different stored values depending on whether the sender has posted in that community before.** | not fixed, registered only | reading-level plus interpreter-checked at this commit: the guard at `:2691`, the branch split at `:2694`, the raw assignment at `:2695` and the stripped one at `:2698` read; `.strip()` confirmed to raise `AttributeError` for `123`, `True` and `['gold']`; the tail `try` located at `:2700`, below both branches. Not covered -- the four flair tests in `tests/test_ap_create_reply.py` send `'  gold  '`, `'gold'`, no key at all, and `''`; none sends a truthy non-string. The strip asymmetry itself IS pinned, by the padded value in the create test and the unpadded one in the update test -- **covered but unregistered**, sub-project 14's "a row covered by a test is not a row discharged" reaching a fourth case |

**Next free number: D297.** D260-D269 were taken by sub-project 15 --
D260 the declined fix, D261 the one defect fixed here, D262-D265 four
registrations, and D266-D269 the four asymmetry-table rows that needed
entries -- and **D270-D273 by its final fix wave**, four unregistered
create-path crashes found by the whole-branch review, three of them further
copies of D256, D247 and D255. **D243 and D257 were NOT renumbered**: they are
sub-project 14's entries, closed by this sub-project's fixes and updated in
place in sub-project 14's own section, each with an appended, marked closure
note. Two cells written by this sub-project were **corrected in place rather
than appended to** -- D257's two closure-note clause pointers (off by one) and
D265's catalogued-cause number (5 for 4) -- because the append-and-mark
convention protects a cell that was true when written, and neither of those
was. **D274-D282 were taken by sub-project 16**, whose section follows this one
and whose subsection 5 indexes the unguarded-peer-input family; **six** of this
section's entries are in that index (D261, D264, D270-D273), listed with their
sites re-read at that commit and **not edited** -- the create-path citations
were still exact, and only the update-path entries in sub-project 14's section
had drifted, uniformly by the three lines this slice's own fixes inserted above
them. **D261 is in the index as the third copy of D237's `"content": null`
crash**, which is the same "a third copy of an already-registered defect" shape
this section records for D270-D272 and is the reason the index exists. **D283 was taken by sub-project 16's final fix wave**, which added it to that index's rejection list; none of this section's entries was touched by it. **D284-D296 were taken by sub-project 17**, which extended the same index to D236-D296 and touched no entry of this section either -- though it is worth noting here, because this section is where the "third copy" shape is recorded, that its **D292** is the same shape a fourth time: sub-project 17 fixed an unguarded `['name']` read on the update path and registered the create-path copy (`app/models.py:2205-2209`) in the same change rather than leaving it for a later slice to rediscover. If you take D297, say so here in the change that takes it.

**Six shapes worth carrying forward from this sub-project's rulings, now in
`tests/README.md` as facts 81-86 plus a corollary appended to fact 56 -- and a
seventh, fact 87, added by the final fix wave: coverage.py emits no arc for a
conditional expression, so ternary arms must be enumerated by reading. That
wave also WIDENED two existing facts rather than adding to the count: fact 75's
cause 4 now admits a second shape of tautology (a condition falsified by an
invariant established before the guard runs, not only a body that rewrites its
own precondition -- D265's shape), and fact 73's first bullet now covers a
CALLEE that re-checks the guard's own condition, which is how sub-project 15's
parent-comment guards needed a distinct third author to be killable.** The six:
an
unfixed crash inside a broad handler is a downstream route to every "nothing
happened" assertion in the function, and the routing around it must be
per-test; a correction does not correct its copies, and the mechanical remedy
is to grep for the **entities the correction names**; grep-for-the-twin is
necessary but not sufficient, because the twin must also *work*; a source read
of a guard is not complete without the session settings it runs under; a
docstring explaining why a fixture is *safe* is itself a claim about
production, and must be written from tracing what a miss would do rather than
from the shape of the code; and a two-conjunct filter needs one negative per
conjunct, each holding the other true. The corollary on fact 56 records the
technique this slice bought its head-guard attribution with: **turn
`LOG_ACTIVITYPUB_TO_DB` on deliberately, restore it, and assert the EXACT
message** -- a substring match gives back the very ambiguity the fixture was
bought to remove.

**Three false claims shipped in this sub-project's own artefacts and every one
was caught, two of them by the agent that wrote them.** A docstring asserting
that a type mismatch would make a test "pass for the wrong reason" had **both
halves** inverted, and its author's diagnosis is the best line of the slice:
it was written "from the shape of the code -- a cast implies a type matters --
rather than from tracing what a miss would do". A helper docstring generalised
"1 failed, 67 passed" into "every test in the module green", **past the 1**, in
the same round that was correcting a different measurement error. And a report
read a `session=session` kwarg off a `PostReply.new` call twelve lines below
the call it was describing -- an attribution error in a report whose subject
was a defect's precise mechanism, the campaign's oldest shape in a third form:
not a wrong function for a line, but a **wrong call for an argument**. The
common remedy is fact 82: **after correcting a claim, grep for the specific
entities the correction names and read what they say** -- the place you
learned the truth is not the only place the falsehood lives.

**One process observation, from a report that stated a negative and was
wrong.** Two evidence errors were found in one Task 6 report: an omission
("both writers", where three exist) and a **denial** ("the only other
NOTIF_MENTION subtype is post_mention", where a third exists). Neither changed
the verdict. But the denial is the worse of the two, because **an omission
leaves the question open and a denial forecloses the search that would have
found the counterexample.** Worth carrying into how reports state negative
claims.

## Sub-project 16: the create path's post half, and the notification fan-out

`docs/superpowers/specs/2026-09-04-coverage-notify-post-16-design.md` and
`docs/superpowers/plans/2026-09-04-coverage-notify-post-16.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-04-coverage-notify-post-16/`, not committed), on
branch `blentz`. Eight tasks -- the plan's seven plus a **Task 7b the
controller inserted after measuring coverage** -- brought three functions in
`app/activitypub/util.py` under test entire: `create_post` (`:2777-2793`),
`notify_about_post` (`:2796-2800`) and `notify_about_post_task`
(`:2804-2936`), all three spans read off an **unfiltered** `^def ` scan at
this commit. **One** defect was fixed, in one commit, `0489dc1d`
(**D275**). Tests live in `tests/test_ap_notify_post.py` (**34 collected, 33
test functions** -- one parametrized x2). `app/activitypub/util.py` measures
**72.5547% blended** (74.41% statements, 69.23% branches) after this
sub-project, up from 71.0362%; the floor rises **71 -> 72**. Full suite after
this sub-project: **3646 passed, 3 skipped, 6 subtests passed** in 323.38s.

**THE FIRST SUCCESS CRITERION IS MET IN THE STRONGEST FORM THIS CAMPAIGN HAS
RECORDED, AND THE LAST THREE STATEMENTS WERE CLOSED BY A TASK THE PLAN DID NOT
CONTAIN.** Uncovered statements in `app/activitypub/util.py:2777-2936` fell
from **53 to zero**, with **zero** uncovered branches. Sub-project 15 met this
criterion with zero *reachable* uncovered statements and two proved dead by
construction; this slice leaves **no remainder at all**. The last three were
`:2932-2934` -- `except Exception:` / `session.rollback()` / `raise`, the tail
handler of `notify_about_post_task` -- and the plan assigned them to no task,
which is a plan gap rather than a decision. The controller inserted **Task 7b**
after the coverage run and it reached them by a **real, uninjected, peer-caused
condition** rather than a patched exception. **Note what the ordering bought:
the plan's task list was complete against the plan's own enumeration and still
left a region uncovered, and only the measurement said so** -- sub-project 15's
lesson ("a criterion stated against a bounded region and then actually measured
catches its own author's arithmetic") reaching a second slice, this time
catching an omission rather than a miscount.

**AND THE TASK THAT WAS INSERTED FOR COVERAGE PRODUCED THIS SLICE'S LARGEST
FINDING, WHICH HAS NOTHING TO DO WITH COVERAGE.** Task 7b needed a real failure
mid-fan-out and found one: `user.unread_notifications` is nullable with no
backfill, so every `user` row predating one 2023 migration still holds NULL and
**49 unguarded `+= 1`/`-= 1` sites across `app/` crash on it**. That is
**D274**, registered not fixed, and it is the most consequential thing in this
slice. It was found because the task went looking for a state production can
actually reach instead of patching `session.query(User).get` to return `None`.
**The general shape, which is worth more than the entry: a coverage task told
to reach an exception handler will find either an injection or a defect, and
insisting on the second is what turns a coverage chore into a finding.**

**THE PLAN'S ONE AUTHORISED FIX WAS FOR A DEFECT THAT IS LATENT, NOT LIVE, AND
THE DISCOVERY CHANGED THE FIX'S VERIFICATION INSTRUMENT RATHER THAN CANCELLING
IT.** The plan and spec both describe the `NOTIF_FEED` arm's misplaced
`notifications_sent_to.add` as a live suppression bug and Task 7 as inverting a
pinned assertion. Task 5 proved from source, and the controller and two
reviewers each re-derived independently, that **no conjunct of that arm's guard
is feed-dependent**, so the misplacement can only ever pre-empt a rejection
that was coming anyway and **no pin of any shape could have been written that
the fix would invert**. Ruling J reframed the gate: the fix still lands,
because an equivalence proof is a *stronger* claim than a failing test rather
than a weaker one, and its verification became a re-run of **all 70 mutations
from Tasks 2-6's tables against both the pre-fix and the post-fix code with the
same 33-test file** -- 0 of 70 differed on `(passed, failed, failing-test names,
exception kinds)`. **The operational rule: when a fix is provably
behaviour-preserving, "prove it by a mutation that fails a named test" is
unsatisfiable by construction, and the instrument that replaces it is the
accumulated mutation table run against both versions.** That is now fact 93 in
`tests/README.md`.

**THE PLAN BUILT IN FIVE STEPS THAT SAY "READ X AND REPORT WHAT YOU FIND", AND
FOUR OF THE FIVE CAME BACK AGREEING WITH IT.** The filter-set table was
confirmed row by row from source, each arm read by its own task (Tasks 2-5);
the ternary enumeration came back at **six**, the spec's number, independently
re-derived by an AST walk at review (Task 6); `Post.new`'s count of one
explicit `raise` was confirmed (Task 1, `app/models.py:2097`); and the topic
lookup's unreachability was re-derived rather than inherited (Task 4, **D280**)
-- though with one correction to the plan's prediction, since forcing the guard
False raises `UnboundLocalError`, a `NameError` **subclass**, not the bare
`NameError` the plan named. **The fifth is this register task's own, and it
DISAGREED**: the plan's family list was called thirteen where its own
enumeration held fourteen, and omitted a member -- and this task's own first
answer then omitted two more, which its review caught (subsection 5).
**Recording the four confirmations matters as much as the one correction** -- a
plan that is verified and right is evidence the verification step is cheap, not
evidence it was unnecessary, and the one step that disagreed was the one whose
answer no earlier task would have re-derived.

**THE VACUITY THIS SLICE FOUND IS INVISIBLE TO COVERAGE, TO MUTATION SCORE AND
TO A GREEN SUITE, AND IT IS STRUCTURAL IN THIS HARNESS.** `tests/conftest.py:143`
truncates with `RESTART IDENTITY`, so every sequence restarts at 1 in every
test; `_seed_scenario`'s Community and its Post therefore both get primary key
1, and Task 3 measured an assertion on `targets['community_id']` to be mutable
to `post.id` **with the whole file still passing**. Tasks 4 and 5 hit the same
collision on `topic.id` and `feed.id` and pre-empted it by seeding explicit
ids and asserting `len({...}) == n`. Now fact 89 in `tests/README.md`.

**THREE TIMES IN THIS SUB-PROJECT A REVIEW'S OWN FINDINGS CARRIED WRONG LINE
NUMBERS, AND THE IMPLEMENTER CAUGHT EVERY ONE.** Three of the four citations in
Task 5's review round were off; Task 6's review placed a production line at
`:2935` when it is at `:2931`. Sub-project 15 saw the same shape. **A correction
carries more authority than what it corrects, so it receives less scrutiny** --
which is exactly backwards. Now fact 88 in `tests/README.md`, and it is the
first fact in this file addressed to how a *review* is read rather than to how a
test is written.

### 1. The most serious finding in this slice, registered not fixed -- D274

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D274 | `User.unread_notifications` (`app/models.py:1023`), added by `migrations/versions/cae2e31293e8_notifications.py:40`; manifesting in scope at `app/activitypub/util.py:2841`, `:2864`, `:2895`, `:2929` and at 45 further sites across `app/` | **Not fixed -- the column is nullable with a PYTHON-SIDE default only, so every `user` row that predates the migration holds NULL, and 49 executable `+= 1`/`-= 1` sites crash on such a row with `TypeError`.** `batch_op.add_column(sa.Column('unread_notifications', sa.Integer(), nullable=True))` (`cae2e31293e8_notifications.py:40`, `Create Date: 2023-11-30`) carries **no `server_default`**, and `upgrade()` (`:19-42`) contains no backfill `UPDATE`; `grep -rln unread_notifications migrations/` returns **that one file**, so nothing later fills the NULLs in. `app/models.py:1023` is `unread_notifications = db.Column(db.Integer, default=0)` -- a Python-side INSERT default, applied when the ORM inserts a row and never to a row it did not insert, so it never repairs an existing NULL. **Measured blast radius, re-counted directly by the controller after the implementer corrected the reviewer's smaller number:** `grep -rn "unread_notifications" app/ --include=*.py` = **78** lines; `grep -rn "unread_notifications *[+-]= 1"` = **55**, of which **six** are the trailing comment `# user.unread_notifications += 1 hangs app if 'user' is the same person` (`app/activitypub/util.py:2463`, `:2513`, `:2550`, `:2579`, `app/api/alpha/utils/community.py:395`, `:466`) -- so **49 executable** attribute-arithmetic sites, several times the review's first enumeration, which listed a correct subset and read as the whole set. **The count that goes in a register is the one that was measured against the working tree, not the one that was enumerated by hand**; the implementer corrected the reviewer here and the controller re-ran all three greps before accepting the correction. Two comparison sites fail the same way: `get_user_unread_count` (`app/api/alpha/utils/user.py:137`) reads the column at `:140` and compares at `:141`, so a pre-migration user cannot fetch their unread counts at all, and `post_private_message_mark_as_read` (`:158`) does the same at `app/api/alpha/utils/private_message.py:172`. **A second, quieter manifestation the register would otherwise miss: seven sites do the arithmetic in SQL, where `NULL + 1` is NULL and nothing raises**, so those fail **silently**, leaving the counter NULL for ever -- `app/cli.py:1166`, `app/community/routes.py:1768`, `app/api/alpha/utils/reply.py:660` and `:664`, `app/utils.py:2927`, `app/admin/routes.py:1779`, `app/api/alpha/utils/admin.py:78`. **A fix that coalesced only the Python sites would leave those seven permanently stuck.** Inside this slice's own region the crash aborts the notification fan-out mid-flight and, under a real Celery worker, fails the task -- which is how the condition was found. **Not fixed because no coalescing spelling exists anywhere in `app/`**: `grep -rn "unread_notifications or 0\|unread_notifications is None\|coalesce(.*unread" app/ --include=*.py` returns nothing, so there is no house style to copy and a fix means choosing behaviour the codebase has never had (a backfill migration, a `server_default`, a `nullable=False`, or a coalescing read at 58 sites). That is register territory by the campaign's own rule. | not fixed, registered only | measured: `test_a_null_unread_counter_mid_fan_out_rolls_back_only_the_failing_recipient` (`tests/test_ap_notify_post.py`) seeds a `user` row with `unread_notifications = None` and a `NOTIF_COMMUNITY` subscription, calls `notify_about_post_task` with **no patching and no injected exception**, and the production code raises `TypeError: unsupported operand type(s) for +=: 'NoneType' and 'int'` by itself. The migration (its `upgrade()` read in full), the model column, all seven SQL sites, both comparison sites with their enclosing function attributions taken from an unfiltered `^def ` scan, and every grep quoted above re-run at this commit |

**The route this entry did NOT take is worth as much as the one it did.** Task
7b's brief nominated a different way into the tail handler: a
`NotificationSubscription` whose user has since been deleted, making
`session.query(User).get(notify_id)` return `None`. **That route is closed, and
closed harder than the implementer first argued.** The implementer's first
argument was that `User.delete_dependencies` (`app/models.py:1490`) deletes the
subscriptions itself at `app/models.py:1535`, which only closes the
application's own deletion path. The reviewer supplied the stronger form:
`migrations/versions/7aee4cb7db24_notification_subscription.py:28` creates the
table with `sa.ForeignKeyConstraint(['user_id'], ['user.id'], )` and **no
`ondelete`**, and `app/models.py:3776` declares the same FK, so a
`create_all`-built schema carries it too -- **PostgreSQL refuses the delete, so
even a raw `DELETE FROM "user"` cannot produce the orphan.** Unreachable by
*any* deletion, not merely by the sanctioned one. Recorded here so the next
slice that wants a `None` user in a notification loop does not re-derive it.

### 2. The one defect fixed, and the finding that changed its character -- D275

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D275 | `notify_about_post_task`'s `NOTIF_FEED` arm (`app/activitypub/util.py:2931`), against the three sibling arms (`:2843`, `:2866`, `:2897`) | **FIXED, commit `0489dc1d`. `notifications_sent_to.add(notify_id)` sat one indentation level out from where its three sibling arms put it -- at 20 spaces, in the `for notify_id` body, rather than inside the `if` that starts at `:2910` -- so a recipient the arm filtered out was recorded as notified.** The production diff is exactly one indentation change, 20 spaces to 24. **The finding that changes this entry's character, and it was the plan's premise: the defect is LATENT, NOT LIVE.** Every conjunct of the guard at `:2910-2914` is constant across iterations of `for feed in community_feeds:` (`:2904`) for a fixed `notify_id` -- the token `feed` appears nowhere in `:2907-2914`, `post` is fetched once at `:2809` and never reassigned, and the one conjunct that can change (`notify_id not in notifications_sent_to`) is monotone true->false. `NOTIF_FEED` is the last arm, `:2932` being the `except`, so nothing downstream reads the set. **So the misplaced `add` could only ever pre-empt a rejection that was coming regardless, and no pin of any shape could have been written that this fix would invert.** **The load-bearing reason for the invariance is not the one Task 7's report gives, and the difference matters for anyone reusing the argument.** That report rests it partly on the three block helpers being `@cache.memoize`-decorated and on "the per-recipient commit touches rows no conjunct reads". Both understate it: `get_task_session()` returns `Session(bind=db.engine)` (`app/utils.py:3673-3675`) with no `session_options`, so `expire_on_commit` is at SQLAlchemy's default `True` and the per-recipient `session.commit()` at `:2930` **does** expire `post` -- the guard's `post.*` accesses are re-SELECTed on the next iteration, and the memo is inert under this suite in any case (`CACHE_TYPE = 'NullCache'`). **The fact that carries the argument is that nothing anywhere in the function WRITES the post row or any of the four block tables (`CommunityBlock`, `InstanceBlock`, `InstanceBan`, `UserBlock`) -- not that nothing reads them.** The only writes inside the loop are `session.add(new_notification)` (`:2927`) and `user.unread_notifications += 1` (`:2929`). **Fixed anyway, and the reason is forward-looking**: the correct spelling exists three times in the same function, the edit is one indentation level, and a correct-by-accident line becomes live the moment a fifth arm follows `NOTIF_FEED` or any feed-dependent conjunct joins the guard. **The invariant matched was "the `add` is the last statement of the `if` body", not the literal 20-space indent -- copying the siblings' indent literally would have reproduced the bug**, since 20 is where the misplaced line already was. **The spec and the plan still describe this defect as present and describe Task 7 as inverting a pin** (`...-16-design.md:165`, `:312`; `...-16.md:403-406`, `:479-483` -- **the plan citation is corrected in place from the `:404` an earlier draft of this cell carried, which was the sentence's second line rather than its first: a pointer that was wrong the day it was written, so it is fixed rather than appended to**). **Every original line of both documents is left exactly as written and the correction lives here instead**: they are dated planning records, the campaign's convention protects a record that was true when written, and the single most instructive thing in this slice is precisely that the plan was wrong about the defect's liveness -- rewriting them would delete the evidence for Ruling J. This entry is where a reader goes for what production does now. **The pointer is made two-way rather than one-way, on the controller's ruling**: each document carries a dated annotation appended after its last original line -- appended, because any insertion above would shift the very line numbers this cell cites -- saying the defect described in it was fixed by `0489dc1d` and naming this entry. A reader who opens either document first is directed here; a reader who opens this entry first is told the documents are unrevised. | **fixed**, commit `0489dc1d` | measured, by the instrument Ruling J substituted for a failing test: all **70** mutations from Tasks 2-6's tables re-run against **both** the pre-fix and the post-fix file with the same 33-test file; **0 of 70 differed** on `(passed, failed, sorted failing test names, exception kinds)`, and the reviewer verified that from the two result artefacts rather than from the prose (identical key sets, the prefix file's mtime preceding the fixed file's, the two bases differing by exactly the one-line hunk). `T5-M18` -- the `add` deleted -- still dies solely to `test_a_feed_subscriber_who_blocked_the_instance_is_skipped_for_every_feed` at the statement's new location, so the fix did not turn a live statement into dead code. Task 5's pin was neither inverted nor deleted: it observes behaviour invariant to the defect, and its docstring now says so in the past tense. Indentation measured, not assumed, on both sides: `:2825`/16 -> `:2843`/20, `:2850`/16 -> `:2866`/20, `:2876`/16 -> `:2897`/20, `:2910`/20 -> `:2931`/24 |

**One stale comment, found while establishing that this arm is live at all and
recorded rather than fixed:** `app/constants.py:58` reads `NOTIF_FEED = 5  # not
actually used anywhere yet, but will be the same as NOTIF_TOPIC`. It is used:
`feed_notification` (`app/feed/routes.py:316`) constructs a
`NotificationSubscription(..., type=NOTIF_FEED)` at `:326-327` -- the only such
construction in `app/` -- and six other modules reference the constant
(`app/models.py`, `app/utils.py`, `app/activitypub/util.py`,
`app/user/routes.py`, `app/api/alpha/utils/user.py` and `app/feed/routes.py`
itself). The comment is a
comment, not behaviour, so it takes no entry; it is noted because a reader
deciding whether the feed arm matters in production will meet it first.

### 3. Four items registered, not fixed -- D276-D279

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D276 | `notify_about_post_task`'s `NOTIF_USER` arm (`app/activitypub/util.py:2823-2824`), against `NOTIF_TOPIC` (`:2873-2875`) and `NOTIF_FEED` (`:2907-2909`) | **Not fixed -- the arm computes `blocked_communities` and `blocked_or_banned_instances` and NOT `blocked_users`, so a subscriber who has blocked an author still receives every post that author makes.** Read from source: `blocked_users` appears nowhere between the `# NOTIF_USER` comment (`:2820`) and the `# NOTIF_COMMUNITY` comment (`:2845`), where both sibling arms below call all three helpers. **The plausible reading is that the omission is deliberate**: this arm's subscription is keyed on the author (`notification_subscribers(post.user_id, NOTIF_USER)`, `:2821`), so its recipients chose to follow that author, and a `UserBlock` on someone you have explicitly subscribed to is a contradictory state that the subscribe path arguably need not honour twice. **The case it leaves open is the one that is not contradictory: the recipient who subscribed first and blocked later -- and the block path DOES delete a subscription, just not that one.** `block_another_user` (`app/shared/user.py:20`) runs `DELETE FROM "notification_subscription" WHERE entity_id = :current_user AND user_id = :user_id` (`:46-48`) binding `current_user` to the **blocker** and `user_id` to the **blocked person** -- so it removes the *blocked person's* subscription to the *blocker*, and leaves the blocker's own subscription to the person they just blocked in place. `block_profile` (`app/user/routes.py:828`) does the identical thing at `:844-846`, and those two are the **only** `DELETE FROM "notification_subscription"` statements in `app/`. Read one way that is a deliberate one-directional policy (blocking someone stops *them* following *you*); read the other, it means the blocker keeps being notified of every post by someone they have blocked, with no surface that stops it, because this arm never consults `blocked_users` either. **Whichever reading is right, the two halves have to agree, and this entry is where the disagreement is recorded.** Not fixed because closing it is a behaviour choice with two defensible answers (honour the block in the arm, or widen the DELETE), not a mechanical misspelling: the sibling arms' spelling is available to copy but the *decision* is not. | not fixed, registered only | reading-level, verified against source at this commit by two tasks independently -- Task 2 read the arm's own two lines and Task 3 read them again from the mirror side; the three sibling arms' helper calls read at `:2848-2849`, `:2873-2875` and `:2907-2909`; the two DELETE statements located by grepping `app/` for the statement text rather than for the words used to describe it, and both read in full with their bound parameters. Not covered: no test in `tests/test_ap_notify_post.py` seeds a `UserBlock` against a `NOTIF_USER` subscriber, because there is no guard there to pin |
| D277 | `notify_about_post_task`'s `NOTIF_COMMUNITY` arm (`app/activitypub/util.py:2848-2849`), against `NOTIF_TOPIC` (`:2873-2875`) and `NOTIF_FEED` (`:2907-2909`) | **Not fixed -- the exact mirror image of D276: this arm computes `blocked_users` and `blocked_or_banned_instances` and NOT `blocked_communities`, so a subscriber who has blocked a community still receives every post in it.** `blocked_communities` appears nowhere between `:2845` and the `# NOTIF_TOPIC` comment (`:2868`). **The plausible reading is the same shape as D276's and is weaker here**: the subscription is keyed on the community (`:2846`), so the recipient chose this community, and a `CommunityBlock` on a community you subscribed to is again a contradictory state. **The case it leaves open is worse than D276's, because here the block path deletes NOTHING and the two states simply coexist.** `block_community` (`app/shared/community.py:85`) adds the `CommunityBlock`, commits and clears the memo -- no subscription cleanup at all -- and the route `community_block` (`app/community/routes.py:1555`) does the same; the `NotificationSubscription ... .delete()` calls that mention `NOTIF_COMMUNITY` (e.g. `app/community/routes.py:1651`) are on the community **ban** path, not the block path. And `subscribe_community` (`app/shared/community.py:394`) refuses only an existing subscription and a ban -- unlike `subscribe_user` it has no self-check and no block check -- so a user can block a community and subscribe to it, in either order, and be notified for ever. **The two omissions together are the register's point: `NOTIF_USER` and `NOTIF_COMMUNITY` are exact complements, each omitting precisely the helper that would filter on the entity the OTHER one is keyed to, while the two arms below them call all three.** A pattern that regular is more likely a shared oversight than two independent policies, but "more likely" is not the standard for a fix. Not fixed for D276's reason. | not fixed, registered only | reading-level, verified against source at this commit by the task that owns the arm (Task 3) rather than inferred from D276; `subscribe_community`'s two refusals read directly at `app/shared/community.py:394`, and contrasted with `subscribe_user`'s self-check (`app/shared/user.py:89`, the check at `:117-118`). `block_community` (`app/shared/community.py:85`, its whole body read) and `community_block` (`app/community/routes.py:1555`, its add/commit/memo-clear at `:1559-1561` read) each read to establish the negative -- that neither deletes a subscription -- rather than inferred from the absence of a grep hit. Not covered, for D276's reason |
| D278 | `notify_about_post_task`'s four per-recipient commits (`app/activitypub/util.py:2842`, `:2865`, `:2896`, `:2930`) against its single tail handler (`:2932-2934`) | **Not fixed -- the fan-out is not transactional. Each arm commits INSIDE its own recipient loop, so `session.rollback()` in the tail handler discards only the failing iteration and every recipient already reached keeps a durable `Notification` row and a durable counter increment.** A mid-fan-out exception therefore leaves the notification set **half-delivered**, and because the handler re-raises (`:2934`) a real Celery worker retries the task from the top -- re-delivering to everyone an earlier arm already notified, since nothing in the function is idempotent per recipient. **This is a property of the function's transaction shape rather than of its control flow, which is why no guard test could have surfaced it and why it took a task written to reach the handler to find it.** Not fixed because the repair is a choice the codebase has never made: one commit at the end of the fan-out (which changes the failure mode from partial delivery to no delivery), or an idempotency key per recipient (new state), or leaving it. **The sibling half of the pair does the same thing** -- `notify_about_post_reply` commits per recipient at `:2966` and `:3008` -- so there is no correct spelling to copy here either. | not fixed, registered only | measured: `test_a_null_unread_counter_mid_fan_out_rolls_back_only_the_failing_recipient` asserts the split on persisted rows read back after the raise -- the `NOTIF_USER` recipient's `Notification` survives and their counter went 7 -> 8, the `NOTIF_COMMUNITY` recipient has no row and their counter is still NULL, and `Notification.query.count()` is 0 before the call and 1 after, so "one survivor" means one row in the table rather than one row for one user. Two mutations pin the handler's two halves separately: deleting `raise` (`:2934`) fails with `DID NOT RAISE TypeError`, and `session.rollback()` -> `session.commit()` (`:2933`) fails with `assert [<Notification 2>] == []`, each a sole kill |
| D279 | `notify_about_post_task`'s four counter increments (`app/activitypub/util.py:2841`, `:2864`, `:2895`, `:2929`) against `notify_about_post_reply`'s counter maintenance (`:2965`, `:2979`, `:3007`) | **Not fixed -- the post-side task mutates `user.unread_notifications` four times with NO lock, where its reply-side twin wraps every one of its three counter mutations in `redis_client.lock(f"lock:user:{notify_id}", timeout=10, blocking_timeout=6)`.** Read side by side: `:2962` and `:3005` open a per-user Redis lock around the reply function's two `+= 1` sites, and `:2977` opens one around the third, while a case-insensitive `grep` for `redis` over `:2777-2936` returns **nothing** -- the post-side task holds no lock at any of its four sites. (`lock` alone is not the grep to quote: it matches nineteen lines in that region, every one of them inside a `blocked_comms` / `blocked_ints` / `blocked_senders` / `blocked_users` / `blocked_communities` / `blocked_or_banned_instances` identifier and none of them a lock. **Corrected in place, not appended to: an earlier draft of this cell quoted "`grep` for `redis` or `lock` ... returns nothing", which was false as written the day it was written** -- the substantive claim it supported was right and re-measured, but a stated grep result must be one that was run as stated.) Since `notify_about_post_task` runs under Celery and two posts can fan out to the same recipient concurrently, the post side's `+= 1` is an unsynchronised read-modify-write on a shared row and can lose an increment. **The file states the intent it is failing to apply**: the comment `# user.unread_notifications += 1 hangs app if 'user' is the same person` appears at six sites (`:2463`, `:2513`, `:2550`, `:2579`, `app/api/alpha/utils/community.py:395`, `:466`), so contention on this exact column is a known hazard elsewhere in the same file. **A second, narrower asymmetry in the same comparison, recorded so it is not mistaken for the first**: the reply twin also *recounts* in one branch -- `user.unread_notifications = Notification.query.filter_by(user_id=user.id, read=False).count()` (`:2979`) -- which the post side has no copy of. That recount is **not** a locked increment done differently: it repairs the counter of the **replier themself** after the same branch has marked the parent's notifications read (`:2969-2975`), and the post-side task never marks anything read, so it has no such repair to make. The asymmetry that needs a decision is the lock, not the recount. Not fixed because adding a lock to four sites is new behaviour on a hot path with its own documented hang risk, and the campaign's rule is that a fix copies a spelling rather than choosing one. | not fixed, registered only | reading-level, verified against source at this commit: all seven counter sites read side by side with their surrounding statements, the three `redis_client.lock` calls read at `:2962`, `:2977` and `:3005`, and the absence of `redis` over `:2777-2936` established by grepping the region rather than by not noticing one, and the nineteen `lock` matches read to confirm every one is a `blocked_*` identifier. Not covered: the four post-side increments are each pinned by a `+= 1` -> `= 1` mutation (seeded 7, asserted 8) in `tests/test_ap_notify_post.py`, which pins the arithmetic and says nothing about concurrency; no test in this suite runs two fan-outs at once |

### 4. Three findings that are not production defects -- D280-D282

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D280 | `notify_about_post_task`'s conditional topic lookup (`app/activitypub/util.py:2869-2872`, `:2885`) | **Not a defect -- an ordering smell with NO reachable failure, and the reachability verdict is the entry.** `topic_send_notifs_to = notification_subscribers(post.community.topic_id, NOTIF_TOPIC)` (`:2869`) runs **before** the guard `if post.community.topic_id:` (`:2870`) that binds `topic` (`:2871`), so on paper a community with no topic enters the loop at `:2872` with `topic` unbound and dies at `'topic_name': topic.name` (`:2885`). **It cannot.** `notification_subscribers` (`app/utils.py:2936-2939`) is `db.session.execute(text('SELECT user_id FROM "notification_subscription" WHERE entity_id = :entity_id AND type = :type '), {...})` -- **raw `text()` SQL with a BOUND parameter**, so SQLAlchemy's `column == None` -> `IS NULL` rewrite, which applies only to Core expressions, never reaches it. A Python `None` therefore arrives at PostgreSQL as `NULL`, the predicate is `entity_id = NULL`, which is `NULL` and not `TRUE` for **every** row including a row whose own `entity_id` is `NULL`, and `WHERE` keeps only `TRUE`. So the list is empty for any table contents whatsoever and the loop body is never entered on that path. **Catalogued as fact 75 cause 4(b)** -- tautology by an invariant established by the immediately preceding statement -- rather than cause 5, because it is the preceding statement and not the column's domain that makes the branch dead. **One correction to the plan's prediction, recorded because a later task would otherwise grep for the wrong string**: forcing the guard False raises `UnboundLocalError`, a `NameError` **subclass**, not the bare `NameError` the plan named. Two adjacent possibilities checked and clean: a truthy-but-dangling `topic_id` cannot occur (`Community.topic_id` is `db.ForeignKey('topic.id')`, `app/models.py:582`), and `topic_id == 0` cannot satisfy that FK. | not a defect; verdict recorded | measured and re-derived twice independently -- by the task that owns the arm and by its reviewer, neither inheriting Task 1's passing remark. The task additionally ran a throwaway probe (not committed) inserting two `NOTIF_TOPIC` subscriptions, one with `entity_id=None` and one with `entity_id=1`, and confirmed `notification_subscribers(None, NOTIF_TOPIC) == []` while the other returns its subscriber -- so even the `NULL`-valued row is not matched, which is the case the argument turns on. `UnboundLocalError` at `:2885` captured from mutation M17 |
| D281 | `notify_about_post_task`'s tail handler (`app/activitypub/util.py:2932-2934`) -- a **test-suite** finding, and a **fourth site of D231's shape** | **Not a production defect. Two statement-level EQUIVALENT MUTANTS, and neither is a fixture gap.** (a) **Deleting `session.rollback()` (`:2933`) fails nothing**, for the reason D231 already established at three sites in the refresh tasks and this entry confirms at a fourth: `finally: session.close()` (`:2935-2936`) runs on the exception path whether or not the rollback is there, `Session.close()` expunges and closes the transaction, and the pool's `reset_on_return` default is `rollback` -- so **no path after the exception ever commits the failing iteration's work** and no persisted-state assertion can distinguish the two. The empirical corroboration is worth recording precisely because it is *not* a kill: removing `session.close()` as well does not expose it -- the leaked session sits `idle in transaction` holding the uncommitted INSERT's locks, the `db_session` teardown's `TRUNCATE` blocks on them, and the run **hangs** rather than failing. The rows still never become visible. (b) **A new half D231 does not cover: narrowing `except Exception:` to `except ValueError:` (`:2932`) also survives**, because the `TypeError` propagates either way and `finally` still ends the transaction -- so the handler's *breadth* is unpinnable by persisted state for the same reason its *body* is. What the test does pin is the `raise` and the rollback's semantics, both by sole kills (see D278). **These are the specimens behind fact 75's new cause 6**: fact 75's five causes are all causes of an unkillable **clause**, and these are unkillable **statements**, so the implementer named the nearest analogue rather than force-fitting one -- correctly. | recorded; the statements are covered and the non-killability is proved rather than papered over | measured: `session.rollback()` deleted -> 34 passed; `except Exception:` -> `except ValueError:` -> 34 passed; both deletions plus `session.close()` -> `pass` -> the run hangs, with the blocking pair read straight out of `pg_stat_activity`. Contrast with the two mutants that DO die, recorded under D278. `app/activitypub/util.py` restored and `git diff -- app/` confirmed empty after every mutation |
| D282 | `app/activitypub/util.py:18` -- an **observation about the file**, not a defect in it | **Not a defect -- `from flask import current_app, request, g, url_for, json` binds Flask's `json` over the standard library's for the WHOLE module, so every `json.dumps` in `app/activitypub/util.py` SORTS KEYS.** That Flask's sorts and the stdlib's does not is **measured from the failing assertion that exposed it, not read out of Flask's source** -- the two serialisers were handed the same dict and produced differently-ordered strings, and the import at `:18` is what says which one the module used. The consequence a test author meets is that `log_incoming_ap`'s write of `ActivityPubLog.activity_json` is key-sorted, so an assertion comparing that column against `json.dumps(document)` built with the **stdlib** `json` in a test module fails on ordering alone, with a diff that looks like a content mismatch. **It produced this sub-project's only genuine RED**: Task 6's conditional-expression test failed on first run for exactly this reason, and the repair is to assert `json.loads(column) == document`, which pins what the column is for and leaves ordering -- which no test here is about -- unpinned. Recorded rather than fixed because renaming the import is a whole-module change with no defect behind it, and because the trap is live for any future test in any file that asserts on a string this module wrote. Now fact 92 in `tests/README.md`. | not a defect, observation recorded | measured: the failing assertion and its diff captured on Task 6's first run, then the import read at `app/activitypub/util.py:18` to find the cause rather than guessed at. Read back at this commit |

### 5. The unguarded-peer-input family -- an index, taking no numbers and changing no entry

**Why this section exists.** The family now spans **seventeen** entries -- five
of them fixed (D236, D237, D238, D257, D261) and twelve still open --
registered by sub-projects 14 and 15 and scattered over six subsections of
two sections of this file, and this campaign has twice
registered a defect whose sibling was already registered. This is a navigation
aid and nothing else: **no entry below is renumbered, moved or edited**, and no
D number is taken by it.

**Three disagreements -- two with the plan that commissioned it, and one with
the first draft of this index, which is the more instructive of the two sources,
because an index that asserts completeness is only as good as the sweep behind
it.** (**Corrected in place**, and the correction belongs in the record rather
than in silence: this lead-in read "Three disagreements with the plan that
commissioned it, and one with the first draft", which under the natural parse
claims four and under the charitable one attaches the wrong number to the wrong
source. **It is a prose count contradicted by its own enumeration -- in the
paragraph whose first item catches the plan doing exactly that**, two sentences
below. Nothing derived from it changes: the enumeration, the table and every
count elsewhere were right.)
(1) The plan called the family **thirteen** entries and then enumerated
**fourteen** -- D236, D238, D245, D246, D247, D255-D259, D264, D270-D272 is
5 + 5 + 1 + 3. The prose count was wrong, not the enumeration. (2) **D273 is a
member the enumeration omits.** Its own cell opens "no precedent entry, and no sibling
either", which is true of its *sibling* -- no other function in
`app/activitypub/util.py` writes a `UserFlair`, `UserFlair(` being constructed
exactly once, at `:2697` -- but not of its *family*: the guard at `:2691` is
membership plus truthiness with no type check, and
`request_json['object']['flair'].strip()` then raises `AttributeError` on a
truthy non-string, which is D258's shape (`.upper()` on a null `name`) exactly.
It is listed below as a member, marked as added by this index.

**(3) D237 and D261 were missed by this index's own first draft, and the review
of it caught them.** Both satisfy the test stated below and each says so in its
own cell: a `"content": null` -- which D237's cell records that Mastodon and
Lemmy both send on some edits -- reaches `.startswith` and raises
`AttributeError: 'NoneType' object has no attribute 'startswith'` out of
`update_post_reply_from_activity` (D237) and out of `create_post_reply` (D261).
**Neither of the two exclusion rules that might have covered them survives
contact with this table**: "fixed" cannot, since D236, D238 and D257 are listed
here as fixed; and "the guard is present but too narrow" cannot, since that is
exactly D257's caveat, carried here rather than used to exclude it. Two further
reasons they are added as rows rather than footnoted out. **D258 -- already a
member -- names D237 as its own precedent**, its finding being that "the
null-check was never extended to the sibling field twenty lines below"; an index
that lists the descendant and omits the ancestor is incoherent on its own
evidence. And **D261 is this index's best specimen**: it is the campaign
registering a *third* copy of an already-registered crash, which is the exact
duplication the index exists to surface, so putting it in a footnote as "out"
would defeat the index in the one case that most justifies it. **The lesson is
about the sweep, not the two entries.** The first draft enumerated the family by
following the cross-references the entries make to each other -- D255's naming
sentence, D259's "of the D236 family", D264's "extends the family" -- which
finds every member that *cites* the family and misses every member that predates
the name and never mentions it. **A membership test is only applied when it is
applied to every candidate; walking the citations is walking a subset.** The
re-sweep that followed did apply it, entry by entry, to every register row whose
function is one of the create/update/notify path's five (see the note below the
table), and returned exactly these two.

**The membership test used.** A peer-supplied value is read -- subscripted,
attributed or passed to a callee -- without the guard that read needs, so a
document a peer is free to send raises out of the function. That is the test
D255 applied when it named the family, and it is deliberately about the *shape
of the read*, not about the exception class: these seventeen raise `KeyError`,
`TypeError`, `AttributeError` and `StopIteration` between them -- and
`IndexError` among the antecedents named below -- so **a reviewer grepping the
family for `KeyError` would find eight of the seventeen**, which is part of why
D255 survived twelve tasks.

**Line numbers below are read at THIS commit and the entries' own numbers are
left as they are.** Every family member in `update_post_from_activity` and
`update_post_reply_from_activity` cites lines **three lower** than the file now
reads, uniformly, because sub-project 15's two create-path fixes shifted
everything below them; D270, D271 and D272 already record that shift for the
three entries they extend, and it was checked line by line here and holds for
the rest. **One exception, worth knowing before anyone "fixes" it: D257's
appended closure note was written AFTER those fixes, so its `:3062`/`:3063` and
`:3268`/`:3269` pointers are exact at this commit while its own header
citations (`:3058-3061`, `:3264-3267`) are three lower -- a single cell whose
two halves are dated differently, which is what an append-and-mark convention
produces and is not an error.** **D237 is the drift's sharpest illustration and
the reason this paragraph is not pedantry:** it cites the reply content gate at
`:3011`, and `:3011` is now `def update_post_reply_from_activity(reply:
PostReply, request_json: dict):` -- the drifted pointer landed on a line that
still reads as if it belonged to the claim. The gate is at `:3014`. The
create-path entries (D261, D264, D270-D273) are
exact at this commit. **Nothing here is a correction:
those cells recorded what was true at their own verification time, which is the
case the append-and-mark convention protects, and every function attribution
was re-verified separately from its line range against an unfiltered `^def `
scan.**

| # | site, read at this commit | what a peer sends | raises | status | sibling copies |
|---|---|---|---|---|---|
| D236 | `update_post_from_activity`'s Markdown `source` arm, guard `:3145-3147`, read `:3148` | `source` with `content` and no `mediaType` | `KeyError` | **fixed**, `5cb4f335` | the reply twin's guard (`:3018-3019`) was the spelling copied; the *read* the fix admits is D247 |
| D237 | `update_post_reply_from_activity`'s content gate, now at `:3014` | `"content": null` | `AttributeError` | **fixed**, `e9c38153` | the post twin (`:3143`) has always carried the null check and was the spelling copied; the create-path copy was **D261**, left behind by this fix and taken a slice later. **This is the family's ancestor case and the index omitted it until a review caught it** -- see the note below the table |
| D238 | `update_post_from_activity`'s tag loop, `json_tag['type']` at `:3219` and `:3225` | a tag entry with no `type` key | `KeyError` | **fixed**, `46c16385` | the third comparison in the same loop (`:3231`) and the reply twin (`:3069`) already guarded; `create_post_reply`'s tag loop guards it too (`:2684`) |
| D245 | `update_post_from_activity`'s type dispatch, `:3273` | an `Update` whose object has no `type` | `KeyError` | not fixed | post-only: the reply function never reads `type`. Its practical cost is visible in every post-side `_update(...)` payload in `tests/test_ap_update_pair.py`, which all thread `type='Note'` |
| D246 | `update_post_from_activity`'s `Hashtag` arm, `json_tag['name']` at `:3220` and `:3222` | `{"type": "Hashtag"}` with no `name` | `KeyError` | not fixed | post-only; sits directly behind the guard D238 added |
| D247 | `update_post_from_activity:3148` and `update_post_reply_from_activity:3020` | a markdown `source` with no `content` | `KeyError` | not fixed | **symmetric across the update pair**, so the sibling comparison cannot find it; third copy on the create path is **D271** |
| D255 | `update_post_from_activity`'s `contentMap` fallback, guard `:3203`, read `:3204` | `"contentMap": {}` | `StopIteration` | not fixed | its own "post-only" scope is corrected by **D272**, the create-path copy; the reply *update* function has no `contentMap` arm (D253) |
| D256 | `update_post_from_activity:3200-3202` and `update_post_reply_from_activity:3025-3027` | `"language": {}` or `{"type": "Language"}` | `KeyError` | not fixed | **symmetric across the update pair**; third copy on the create path is **D270** |
| D257 | `update_post_reply_from_activity:3062-3063` and `update_post_from_activity:3268-3269` | `"updated": 123` -- any non-string | `TypeError` | **fixed by sub-project 15**, `fad7af91` | symmetric across the update pair; **no create-path copy** -- `create_post_reply` parses no `updated`. Different in kind from the rest: the `except ValueError` proves the author intended a bad `updated` to degrade, so the guard's domain was one type too narrow rather than absent |
| D258 | `update_post_from_activity`'s title handling, `'name' in` at `:3164`, `.upper()` at `:3188` | `"name": null` | `AttributeError` | not fixed | post-only (the reply function has no title). Note `post.title = None` at `:3187` runs **before** the raise |
| D259 | `update_post_reply_from_activity`'s attachment loop, `'href' in attachment` at `:3050` | `"attachment": [1]` -- a non-string scalar *entry* in a well-formed list | `TypeError` | not fixed | create-path twin is **D264**; the post update function has no attachment handling in the scoped region. Distinct from D251, which is a scalar at the *top level* and is a benign no-op |
| D261 | `create_post_reply`'s content gate, `:2633`, `.startswith` at `:2634` | `"content": null` | `AttributeError` | **fixed**, `acdcf98b` | **the third copy of D237's defect**: sub-project 14 fixed the reply-update half from the post-update half and left the create half carrying it. Raised **above** the tail `try` (`:2700`), so it propagated out where D243's crash in the same function was swallowed -- two mirrored crashes, opposite failure modes |
| D264 | `create_post_reply`'s attachment loop, `'href' in attachment` at `:2668` | as D259 | `TypeError` | not fixed | update twin is **D259**. Raised **above** the function's tail `try` (`:2700`), so unlike D243 it propagates out |
| D270 | `create_post_reply`'s `language` dict arm, guard `:2646`, reads `:2647-2648` | as D256 | `KeyError` | not fixed | third copy of **D256**'s two |
| D271 | `create_post_reply`'s Markdown `source` arm, guard `:2637-2638`, read `:2639` | as D247 | `KeyError` | not fixed | third copy of **D247**'s two |
| D272 | `create_post_reply`'s `contentMap` fallback, guard `:2650`, read `:2651` | as D255 | `StopIteration` | not fixed | second copy of **D255**'s one, and the entry that corrects D255's "post-only" scope |
| D273 | `create_post_reply`'s flair block, guard `:2691`, `.strip()` at `:2698` | `"flair": 123`, `true` or `["gold"]` | `AttributeError` | not fixed | **none** -- `UserFlair(` is constructed once in the whole module, at `:2697`. **Listed as a member by this index**, not by its own cell: the read shape is D258's |

**Antecedents outside the named family, listed so a future task greps them
too.** The family got its name in D255, which scoped it to the create/update
path. The identical shape was registered a sub-project earlier in the three
actor-refresh tasks and is **not** counted above: **D218** (six unguarded reads
of an actor document's "required" keys -- `preferredUsername`, `name`,
`publicKey.publicKeyPem`) and **D219(a)** and **D219(b)** (unguarded subscripts
on peer *list entries*, and `IndexError` on `activity_json['icon'][-1]` /
`['image'][0]` where `isinstance(..., list)` is checked and emptiness is not).
D219(c) was the same shape and **was** fixed, at `c9cc56fc`. If the family is
ever counted as a whole rather than as a create/update-path list, those belong
in the count.

**What "seventeen" is and is not a complete count of, stated so nobody reads
more into it than the sweep supports.** (**SUPERSEDED AS TO ITS RANGE by
sub-project 17, appended here rather than rewritten below: the arithmetic in this
paragraph was complete when written and is complete no longer.** Nine of
sub-project 17's thirteen entries -- D284, D285, D286, D288, D289, D290, D291,
D293 and D294 -- are `update_post_from_activity` and therefore INSIDE the
declared scope below, so "seventeen", "thirty-one" and "D236 through D283
inclusive" are each true only of the state this paragraph describes. The
re-derived figures are **21 members + 36 rejections = 57 in-scope, plus 4
entries whose function is outside the declared scope -- D287 (`edit_post`), D292
(`Post.new`), D295 (`Site.admins()`) and D296 (`tests/factories.py`) -- = 61 =
D236 through D296 inclusive**, and the four new members are D284, D285, D290 and
D291. **The identity this paragraph enjoyed is BROKEN, not merely extended**:
the in-scope set is no longer the whole numeric range, because the campaign has
begun registering entries outside the create/update/notify path, so a future
sweep must state the property that bounded its candidate set rather than the
range it happened to occupy. Full reasoning, member by member and rejection by
rejection, in sub-project 17's section. **Marked and appended rather than
rewritten because a reader who opens this paragraph first must not meet a stale
completeness claim with nothing on the page contradicting it -- which is the
failure mode sub-project 17's own fact 96 describes.**) It is complete over
**every register entry whose function is one of `create_post`, `create_post_reply`,
`update_post_from_activity`, `update_post_reply_from_activity`,
`notify_about_post_task`** -- the create/update/notify path -- each such entry
read against the membership test one at a time rather than followed through the
family's own cross-references. The entries that scope caught and **rejected**,
with the reason, so the next sweep does not re-open them: **D240/D243** (empty
`ids` tuple rendering `IN ()`) are a crash but not a *read* -- the tuple comes
from `reply.path`, a local column, not from the peer document; **D239** and
**D260** (unflushed `Language.id`) write NULL and explicitly raise nothing;
**D251** is traced and recorded as a benign skip, "neither a crash nor silent
corruption", and D259 and D264 each already say so; **D263** is a *handler* that
swallows crashes rather than a read that causes one; **D241, D242, D244,
D248, D249, D250, D252-D254, D262, D265-D269** describe asymmetries,
convergence questions and dead code with no exception between them; and
**D274-D283**, this sub-project's own ten, are in scope because they are
`notify_about_post_task` and `create_post` and are rejected one by one --
**D274**'s `TypeError` is a crash but the NULL it reads comes from a *database
column a migration left unfilled*, not from a peer document, which is D240's
distinction on a different source; **D275** is a misplaced `add` with no read in
it; **D276-D279** are filter-set, transaction and locking asymmetries that raise
nothing; **D280-D282** are a reachability verdict, two equivalent mutants and
an observation about an import; and **D283**, added by the final fix wave, is a
third equivalent mutant -- an identity between two spellings of one integer,
with no peer-supplied read in it at all. **The list is now arithmetically
checkable, which is the point of writing it out:** 17 members + 31 rejections =
48 = D236 through D283 inclusive, every entry in the declared scope accounted
for exactly once. (**Extended in place by the final fix wave, which took D283,
whose function -- `notify_about_post_task` -- is inside this index's declared
scope; "D236 through D282 inclusive" would otherwise have become a false
completeness claim the day D283 landed. The count was RE-DERIVED rather than
incremented**: the seventeen members re-read off the table below, the
thirty-one rejections off this paragraph, then checked against D236-D283 for
overlap, gaps and out-of-range numbers -- none of the three, and the partition
holds. **That re-derivation is fact 94 applied to this paragraph's own
arithmetic**, which is where a completeness claim is likeliest to be
incremented without being re-checked.) It
is **not**
a complete count over the register as a whole: the shape predates the family's
name by a dozen sub-projects -- `find_community`'s `a['type']`/`a['id']` walk
(**D28**), its unguarded `type` read and `.startswith` on a non-string
(**D2**, **D3**), and `actor_json_to_model`'s Group and Feed branches having no
`except KeyError` where Person's does (**D12**, **D14**) are all the same
mistake in other units, all fixed. **A future task widening this index to the
whole register should say so in its heading, because "the family" as D255 named
it is a path, not a mistake.**

**And one relative closer than any of those five, which a function-scoped sweep
structurally cannot see: D35.** `PostReply.new` reads `request_json['type']`
unguarded and raises `KeyError('type')` -- the family's shape exactly -- and it
is reached *through* `create_post_reply`, one of the five functions this sweep
scopes to. It is out on two independent grounds, and the second is the one worth
carrying. **(a)** Its entry's function column names `create_resolved_object` and
`resolve_remote_post_from_search`, so a sweep keyed on the function column cannot
reach it: **the crash is in a callee, and a function-scoped sweep sees functions,
not call trees.** **(b)** Its own cell says "`create_post_reply` swallows it, and
the resolver returns None", so it fails the membership test's *"raises out of the
function"* clause -- and that clause is doing real work across this family rather
than being a formality. D264's crash is raised **above** `create_post_reply`'s
tail `try` (`:2700`) and propagates; D243's is **inside** it and is swallowed into
a `None` return (**D263**); D35's is swallowed the same way. **So there is a
sub-family of unguarded peer reads whose crash the tail handler converts into
silence, and the loud ones are the only ones anyone was ever going to notice** --
D261's cell makes precisely that contrast between two crashes in one function.
D35 was fixed on 2026-08-29. **A task widening this index should widen it on both
axes at once: past the function column into callees, and past "raises out of" into
"raises at all".**

**One candidate this index does NOT claim, stated as a question rather than a
finding.** `create_post` reads `id = request_json['id']` at
`app/activitypub/util.py:2779`, unguarded, and **above** the function's own
`try` at `:2788`, so a `KeyError` there propagates out. Whether a peer can
deliver an activity that reaches it without `id` was **not established** by this
sub-project and no test in `tests/test_ap_notify_post.py` sends such a document.
There are four call sites -- `app/activitypub/routes.py:2341`,
`app/activitypub/util.py:4324`, `:4457` and `app/community/util.py:198` -- of
which `:4457` synthesises the key itself (`:4450`) and `app/community/util.py:198`
passes a peer document but is wrapped in its own `except Exception`. The other
two are the open question. **Recorded as a candidate with its reachability
unresolved, because the alternative is a silent omission and this file's own
rule is that a denial forecloses the search a question keeps open.**

### 6. Added by the final fix wave -- D283

The whole-sub-project review found one Important defect in the tests: the
`NOTIF_USER` arm's happy path asserted its whole `targets` dict, including
`'post_id': post.id`, on a scenario where `_seed_scenario` gives Community and
Post the same primary key -- so mutating `'post_id': post.id` to
`post.community_id` left **all 34 tests passing**. That is fact 89's shape at a
fourth site, and the three arms below it had each been retrofitted against it
while this one, written before the discovery, had not. **The fix is a test
change and takes no number.** What takes a number is what the sweep that
proved the fix turned up beside it.

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D283 | `notify_about_post_task`'s `NOTIF_USER` arm, the `'author_id': post.user_id` entry of `targets_data` (`app/activitypub/util.py:2832`), against the `author` binding at `:2812` | **Not a defect -- a THIRD provable equivalent mutant in this function, and the first found by a sweep rather than met by accident.** Substituting `author.id` for `post.user_id` at `:2832` cannot be killed by any test, because `author` is bound twenty lines above as `author = session.query(User).get(post.user_id)` (`:2812`) and is never reassigned. Established by scanning the whole region rather than by not noticing a rebind: `awk 'NR>=2804 && NR<=2936 && /author/'` returns nine lines -- the comment at `:2811`, the binding at `:2812`, this dict's two keys (`:2832`, `:2833`), a THIRD dict key in the `NOTIF_TOPIC` arm (`:2887`), and four `author_id=` column keywords (`:2835`, `:2858`, `:2889`, `:2923`) -- **corrected in place by sub-project 17, not appended to.** This parenthetical read "five `author_id=` column keywords (`:2835`, `:2858`, `:2887`, `:2889`, `:2923`)"; `:2887` is `'author_id': post.user_id}`, a dict key in the `NOTIF_TOPIC` arm and not a column keyword, so the SPLIT was wrong the day it was written rather than having drifted -- every line number in it is exact at this commit, re-derived by `awk 'NR>=2804 && NR<=2936 && /author/'`, and the total of **nine** is unchanged. D283's verdict is unaffected, and the sentence immediately below already classified `:2887` correctly as "a dict key or a column name", which is why the miscount changed nothing that was derived from it. **The only assignment is `:2812` and the only read of the local is `:2833`**; every other occurrence is a dict key or a column name that happens to contain the word. **The two expressions are therefore the same integer on every reachable path, and no arrangement of seeded primary keys can separate them**, which is the distinction that matters: the sibling substitutions in the same dict ARE killable and were made so, and this one is not killable by anything. **Why this is registered rather than left in a fix report.** It was found while discharging the final review's one Important finding -- that `targets['post_id']` was silently vacuous because `_seed_scenario` gives Community and Post the same primary key (fact 89) -- by sweeping **every** id-valued key in the arm against **every** other id in scope rather than only the one the review named. That sweep is 12 mutants over two keys: eleven are sole kills of `test_a_subscriber_to_the_author_is_notified` and the twelfth is this one. **A future slice mutating this arm will otherwise rediscover it from scratch and, worse, may read the survivor as a coverage gap and write a test that cannot exist.** That is the same service D280 performs for the topic lookup's unreachable branch and D281 for the tail handler's two statements. **The generalisable half: a local bound from an expression is not an independent id, and a `targets` dict that carries both is carrying one value twice.** `community` is bound the same way, once for the whole function, at `:2815` (`community = session.query(Community).get(post.community_id)`), so `'community_id': post.community_id` -> `community.id` at `:2856` -- the NOTIF_COMMUNITY arm's dict, the only `'community_id'` entry in the function -- **is predicted to be the same equivalent mutant, and is recorded here as unswept rather than as measured.** Note it is a *different* mutation from the one sub-project 16 measured on that key: `'community_id': post.community_id` -> `post.id` dies, and did the work of exposing fact 89. | not a defect, equivalence recorded | measured, not reasoned: 21 mutations applied by line address to `app/activitypub/util.py` and each run against the whole 34-test file -- six substitutions of `'post_id': post.id` (`:2829`), six of `'author_id': post.user_id` (`:2832`), three of `'post_title'` (`:2830`) and six over the two name-valued ternaries (`:2831`, `:2833`). **20 died -- 14 of them sole kills of `test_a_subscriber_to_the_author_is_notified` and 6 multi-kills across the four `community_name`/`author_user_name` tests -- and this one alone returned 34 passed.** `app/activitypub/util.py` restored and `git diff -- app/` confirmed empty after every one. The seeded ids that make the other eleven killable are `community.id 1`, `author.id 2`, `subscriber.id 3`, `post.id 4`, verified by temporarily asserting the tuple rather than reasoned from factory order |

**Next free number: D297.** D274-D283 were taken by sub-project 16 -- D274 the
NULL `unread_notifications` hazard, D275 the one defect fixed, D276-D279 four
registrations, D280-D282 three findings that are not production defects, and
**D283 the final fix wave's equivalent mutant, in subsection 6 above**.
**No earlier entry was renumbered, moved or edited by this sub-project**, and
the family index in subsection 5 above takes no number: it lists **seventeen**
existing entries with their sites read at this commit, and records that the
plan which commissioned it counted thirteen where its own enumeration held
fourteen and omitted D273, and that the index's own first draft then omitted
D237 and D261 until its review caught them. **The final fix wave changed two
things in that index and nothing else: it added D283 to the rejection list --
D283's function, `notify_about_post_task`, is inside the index's declared
scope, so leaving it out would have made the completeness claim false -- and
re-derived the partition from 17 + 30 = 47 (D236-D282) to 17 + 31 = 48
(D236-D283), checked for overlap, gaps and out-of-range numbers rather than
incremented.** **Sub-project 17 then took D284-D296 and superseded this paragraph's
arithmetic -- adding no row to the index, and re-deriving its partition to 21
members + 36 rejections + 4 entries outside the declared five-function scope =
D236-D296 -- once more re-deriving the partition rather
than incrementing it, and once more editing no row of the index -- though it DID
edit one cell of this section: D283's parenthetical split of its nine `author`
hits, a residual this sub-project parked for it, corrected in place because
`:2887` is a dict key and not a column keyword, so the split was wrong when
written rather than drifted. The total of nine and D283's verdict are
unaffected. Its extension BREAKS an identity this note's arithmetic quietly
relied on: the declared scope no longer covers a contiguous block of numbers,
because the campaign has begun registering entries in `app/shared/`,
`app/models.py` and the test suite. Subsection 5's arithmetic paragraph now
carries a marked supersession sentence saying so, and the full extension is
described in sub-project 17's own section.** If you take D297, say
so here in the change that takes it.

**Nine shapes carried forward into `tests/README.md`, seven as new facts 88-94
and two as widenings of existing facts.** (**This sentence is corrected in place
from "Six shapes … five as new facts 88-93 and one as a widening", which was
wrong twice the day it was written: 88-93 is six numbers, not five, and there
were two widenings, not one.** A miscount in a summary of a change the same
commit made -- the arithmetic that is easiest to skip is the arithmetic about
your own work.) The new facts: a **reviewer's**
citations fail as often as an implementer's, and a correction receives less
scrutiny precisely because it carries more authority (88); `RESTART IDENTITY`
makes two entities share a primary key, so an id-valued assertion can be
silently vacuous with the whole file green (89); a task that commits **inside**
its recipient loop leaves durable partial state that no rollback removes, which
is assertable and must be asserted per-recipient *and* whole-table (90); four
near-identical guard chains in sequence make fact 72's hazard the dominant one
rather than an edge case, and a stored discriminator is what makes a row
attributable to the arm that wrote it (91); and Flask's `json` shadows the
stdlib's in `app/activitypub/util.py`, so that module's `json.dumps` sorts keys
(92). Fact 93 records Ruling J's instrument: **a provably behaviour-preserving
fix cannot be proved by a failing test, and what replaces that gate is the
accumulated mutation table re-run against both versions.** Fact **94** was added
by this sub-project's own register round, on Ruling O, after a review found that
the family index in subsection 5 had missed two members: **to enumerate a set,
apply its membership test to every candidate; walking the cross-references its
known members make to each other returns a subset and looks like an answer.**
Its two pieces of evidence are the index's first draft, which walked citations
and missed D237 and D261 **although D258 was already listed and names D237 as
its own precedent**, and Task 6's reviewer, which reached the same conclusion
from the other side by re-deriving the ternary enumeration with an AST walk over
every node rather than a grep. The widenings:
**fact 75 gains a sixth cause, scoped explicitly to STATEMENTS where its first
five are scoped to clauses** -- a statement whose only observable effect is
performed unconditionally by code that runs after it on every path, proved by
naming the repeating code rather than by counting failures (D281, and D231
three sub-projects earlier) -- and fact 87 gains the enumeration method a
reviewer used here: an **AST walk** (`ast.parse` -> `IfExp` inside the
`FunctionDef` nodes, extents from `end_lineno`) finds ternaries hidden in dict
literals, f-strings, comprehensions and argument defaults that a textual
`' if .* else '` grep misses.

**Appended by the final fix wave, not folded into the sentence above, which was
true when written: a TENTH shape, fact 95, bringing the new facts to 88-95.**
The fix wave produced one shape of its own, and it is about the fix wave's own
failure mode rather than about the code: **a correction travelling UPWARD --
into a committed file, or into a record nobody downstream will re-open -- is
the least-checked claim in the exchange, because its reader is checking the
correction's claim and not its citation.** Sub-project 16 produced five
citation errors; the four that were caught were all caught by an agent who had
the source open anyway, and the fifth was a correction of a cell that was
RIGHT -- this fix wave reported fact 89's `tests/conftest.py:143` as an
off-by-one and wrote `:142` into a test docstring, against three independent
artefacts that cited `:143` and agreed with each other. Corrected at
`9005a671`. **This is the sibling of fact 94, not a restatement of fact 88**:
88 is about who is fallible and is written for the receiver of a review; 94 is
about how a set is enumerated; 95 is about which direction of travel has no
reader positioned to catch it. Its proximate cause is mechanical and is the
transferable half -- **`sed -n 'A,Bp'` prints no line numbers, so mapping the
first printed line to A is off by one whenever the range opens on a blank
line**, where `grep -n ''`, `awk` with `NR` and `cat -n` cannot fail that way.

## Sub-project 17: `update_post_from_activity`'s type tails

`docs/superpowers/specs/2026-09-04-coverage-update-tails-17-design.md` and
`docs/superpowers/plans/2026-09-04-coverage-update-tails-17.md` (design and
plan; the per-task briefs and reports live in the gitignored workspace
`.superpowers/sdd/2026-09-04-coverage-update-tails-17/`, not committed), on
branch `blentz`. Eight test-writing tasks plus this register round brought the
**tails** of `update_post_from_activity` (`app/activitypub/util.py:3139-3572`,
the extent read off an **unfiltered** `^def ` scan -- next `def` is `undo_vote`
at `:3575` -- and confirmed against `end_lineno` by an AST walk) under test
entire: the `Video`, `Question`/poll, `Event`, attachment, url-change and
suspicious-domain clusters, everything at and below `:3273`. **Two** defects
were fixed, in two commits, `2ff71ebf` (**D284**) and `30a9dcec` (**D285**).
Tests live in `tests/test_ap_update_post_tails.py` (**80 tests**).
`app/activitypub/util.py` measures **76.9025% blended** (78.29% statements,
74.41% branches) after this sub-project, up from 72.5547%; the floor rises
**72 -> 76**. Full suite after this sub-project: **3727 passed, 3 skipped, 6
subtests passed** in 247.33s.

**THE FIRST SUCCESS CRITERION IS MET AND THE ONLY RESIDUAL IS IN A CLOSED
SUB-PROJECT'S REGION.** `update_post_from_activity` has **zero missing
statements** across its whole extent. The only two missing branches are
`[3223, 3225]` and `[3263, 3261]` -- the "lookup returned `None`" arms of
`if hashtag:` (`:3223`) and `if flair:` (`:3263`) -- and **both are above
`:3273`**, in sub-project 14's head rather than in this slice's tails. **The
tails measure at zero statements and zero branches.** Those two arms and a third
item are registered together as **D293**: they are three things sub-project 14's
own success criterion could not catch inside a region it closed.

**THE CONTROLLER ASSERTED A SOURCE FACT THREE TIMES AND IT WAS FALSE, AND THE
IMPLEMENTER CAUGHT IT BY MEASURING RATHER THAN INHERITING.** The plan states at
`docs/superpowers/plans/2026-09-04-coverage-update-tails-17.md:97-101`,
`:707-712` and `:904-906` that `Site.admins()` treats `User.id == 1` as an
admin without any role row. It does not: `.join(user_role)` is an **INNER**
join, so a roleless user is eliminated before the `or_` is evaluated. That is
**D295**, and the reason it is registered rather than filed as a process note is
that the claim is a reachability fact about production code which the next slice
to seed an admin fixture will need. **The instruction that caught it was already
standing** -- "establish this from source before seeding" sits in the same
paragraph as the false claim -- and the implementer followed it. **A brief that
supplies a fact and also tells the reader to verify it is not redundant: the
second half is what makes the first half safe to write.**

**THE SLICE'S TWO LARGEST FINDINGS BOTH BECAME WORSE UNDER MEASUREMENT, NOT
BETTER.** The `Notification.targets` JSON-serialisation crash (**D286**) was
first reported as "it crashes"; the measured consequence is a **half-applied
`Update` plus an orphaned `File` row**, because an earlier commit has already
flushed the title, url and type. The `endTime` defect (**D290**) was first
reported as a shared misspelling with the `Event` block's `fromisoformat` twin;
measurement against the running stack showed the twin **preserves the instant**
and only `:3338` discards the offset, so the two are **not twins in outcome**
and the register entry is narrower and sharper than the report that raised it.
**In both cases the correction came from the implementer re-measuring its own
claim after a reviewer questioned it, not from the reviewer supplying the
answer.**

**A PROSE-CORRECTION ROUND THAT WAS SUPPOSED TO CHANGE NOTHING FOUND A LIVE
BUG.** Task 7's second fix round existed only to narrow an over-broad sentence
about `app/shared/post.py:582`. The reviewer's proposed narrowing assumed
`community_member.is_local()` gates the mod row; the implementer checked the
class instead of accepting the mechanism and found that **`CommunityMember`
defines no `is_local`** (**D287**). Verified three times independently. The
sentence that was being polished was wrong about something nobody had thought to
question, and the finding it produced changed the severity analysis of D286 as
well.

**FOUR TIMES IN THIS SUB-PROJECT, PROSE WRITTEN TO REPAIR A FALSE CLAIM
INTRODUCED ANOTHER ONE** -- three of them citation errors in the same task, and
one a false test-attribution reasoned from a class's *name* rather than read off
its call sites. Sub-projects 15 and 16 saw the shape too. The new half this
slice adds is the mechanism behind the *first* occurrence, and it is now fact 96
in `tests/README.md`: a correction that only **deletes** a false claim leaves
nothing on the page saying why the natural phrasing is wrong, so the next writer
**regenerates it from scratch** with nothing to copy from. That is not fact 82
(a correction does not correct its copies): here there was no copy at all.

**THE RIGHT RESPONSE TO ONE STALE ROW IS TO RE-SWEEP THE TABLE.** Task 6's
review named two mutation rows with stale kill counts. The implementer re-swept
all **35** rather than patching the two, on the reasoning that a test added
mid-table invalidates every row measured before it, and found **twelve** stale
plus one sole -> multi status change. Totals were unaffected, which is exactly
why patching would have looked like a fix. Now fact 98.

### 1. Two defects fixed, test-first with mutation-proved tests -- D284-D285

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D284 | `update_post_from_activity`'s poll block -- the Edit-path recreate loop (`app/activitypub/util.py:3346-3351`) and the totals loop (`:3356-3365`), against the counting loop above them (`:3323-3331`) | **FIXED, commit `2ff71ebf`. Both loops re-iterated the peer's `votes` list with NO guard, where the counting loop three statements above filters the same list on three keys -- so `KeyError: 'name'` on a document a peer is free to send -- raised pre-fix at the lines that are now `:3349` and `:3363`, both quoted verbatim from failing runs.** LIVE, not latent: both tracebacks were quoted from a failing run before the fix. **The design decision is the entry, and it is a refusal to copy all three guards.** Each loop is guarded by **what it reads**: the recreate loop takes the `name` guard alone, because it dereferences nothing else; the totals loop takes all three, because `:3361` reads `vote['replies']` and `:3365` reads `['totalItems']`. Copying all three into the recreate loop would have been the *uniform* repair and a destructive one -- for the **ordinary** Edit shape (every choice named, no `replies` key, which is what a poll edit looks like) the second guard would `continue` on every entry, the two `DELETE`s at `:3341-3343` would already have run, and `:3352` would **commit an empty choice set**. That case works today. **The pre-fix severity claim is corrected here and must not propagate**: the report called the all-nameless crash "destroys every vote row and every choice row", which is not supported once the call path is traced. `update_post_from_activity` is reached for an inbound `Question` `Update` from `process_new_content` (`app/activitypub/routes.py:1264`) inside `process_inbox_request`'s `try`, which ends `except Exception: session.rollback(); raise` (`app/activitypub/routes.py:1909-1911`); `:3352`'s commit is never reached, so the `DELETE`s are rolled back. **On the inbox path this was a failed, rolled-back `Update` -- materially closer to a lost `Update` than to permanent data loss.** The Flask-route call site (`app/post/routes.py:2256`) was traced only to Flask-SQLAlchemy's default teardown rollback. **`sort_order` stays contiguous, and that was checked rather than assumed**: `i += 1` is inside the loop body, so `continue`'s placement decides whether a skipped vote leaves a gap, and the `continue` sits **above** the increment. Every consumer of `sort_order` in `app/` is an `ORDER BY` key (`app/activitypub/util.py:185`, `app/shared/tasks/pages.py:227`, `app/post/routes.py:260`, `:702`, `:1126`, `app/api/alpha/views.py:275`, the last also passing the raw integer into the API payload); `grep sort_order app/templates/` is empty. **So ordering survives a gap and consumption permits either placement -- the tie was broken by how the field is written elsewhere**, the create path at `app/models.py:2205-2209` being the same idiom line for line. **The sibling this fix does not reach is D292.** | **fixed**, commit `2ff71ebf` | measured: two tests written first and quoted failing with the two `KeyError`s, plus a third that passes on both sides of the fix and exists to pin the *guard-breadth* decision, which is otherwise invisible to the suite. Ten mutants of the new guards, each killed, and reverting any one guard fails exactly its own test. **The re-run gate was met in full**: all 62 mutations from Tasks 1-3 re-applied against the post-fix file, none unkilled, every "sole" attribution preserved; three of them (T2-M8, T2-M9, T2-M17) changed kill KIND from crash to assertion by the same killers, which the implementer's first delta table under-reported and the review completed. The guard's three tracebacks were established by reverting each guard in turn and reading the output rather than by reasoning -- and that measurement corrected a docstring: reverting the second guard raises `KeyError: 'replies'` at the **third guard's own read**, two statements before the line first credited |
| D285 | `update_post_from_activity`'s `Event` block, the nested image url (`app/activitypub/util.py:3391-3392`), against its guarded twin 95 lines below (`:3487-3488`) | **FIXED, commit `30a9dcec`. `:3391` tested `'image' in request_json['object']` and `:3392` then read the nested `['url']` unguarded, so `"image": {}` or `{"type": "Image"}` raised `KeyError: 'url'` out of the function.** The repair copies the twin's second conjunct verbatim -- one line, no new expression, `if 'image' in request_json['object'] and 'url' in request_json['object']['image']:` -- and the resulting behaviour for a urlless dict is not a new choice either: it falls to `:3398`'s `else` and is treated as "no image", exactly as `:3487`'s `else` already treats it. **Landed under a ruling, in its own commit, because the plan's expectation ("Task 4 owns the only production change") is weaker than the campaign's binding constraint that defects in these functions are fixed test-first.** **The behaviour WIDENING this fix causes, recorded rather than denied, and the reason it is in the cell and not a footnote: the reviewer's mechanism for it was wrong and the implementer corrected it.** `'url' in "https://..."` is a **substring** test and never raises, so the five shapes measured in a bare interpreter are: a dict without `url` -> `KeyError` before, handled after (the repair); a string **lacking** the substring `url` -> `TypeError` before, **silently dropped** after (**a widening**); a list -> the same (**a widening**); a string **containing** the substring `url`, e.g. `.../url.png` -> passes the guard both before and after and still raises `TypeError: string indices must be integers` (**unchanged**); `null` or a number -> the **guard itself** raises `TypeError` (**unchanged**). **So the crash-to-silent-skip widening covers exactly two of four non-dict shapes, and which two turns on whether the peer's url happens to contain the literal substring `url`.** That is **fact 71's own prediction** arriving as a worked case rather than a new discovery -- the paragraph beginning "The other direction is worse because it appears later" already names a membership check added in front of a subscript turning a previously-crashing string input into a silent skip, and calls it a real behaviour widening to record. **The residue is a JOINT finding at both sites, not at `:3391` alone**: `:3487`'s guard is itself only correct for the dict case, so the mechanical copy closed the missing-key hole and **left the non-dict-image hole open at both sites**. Closing that needs `isinstance` checks at `:3391` and `:3487` plus a decision about the array form ActivityPub permits -- new behaviour, at two sites, so it is not mechanical. The widening is a deliberate trade for uniformity with `:3487` and **must not be reverted without also changing `:3487`**. | **fixed**, commit `30a9dcec`; residue open at `:3391` and `:3487` | measured: RED quoted as `KeyError: 'url'` at `:3392`. Mutation M79 (drop the `'url'` conjunct) is a SOLE crash-kill by `test_an_image_with_no_url_is_treated_as_no_image_at_all`, its soleness established by **construction** rather than by observation -- every `_event_update` call in the module enumerated, and only one passes a urlless image dict -- and the test proved non-vacuous by its independent timezone assertion. The new two-conjunct guard was additionally mutated at **conjunct** granularity, all four directions: conjunct 1 -> `False` and -> `True` (the latter crashing `KeyError: 'image'`), conjunct 2 -> `False` and -> `True` (the latter the sole `KeyError: 'url'`, i.e. M79 at finer grain). **Fact 74 discharged, late but complete**: the two mutations previously recorded against `:3391` were re-run after the fix -- one still dies multi(2), the other gained a third killer, so no kill was vacated. The five-shape table reproduced independently in a bare interpreter by the re-reviewer, every string and exception message matching, including the 3.12+ "not a container or iterable" phrasing |

### 2. Seven items registered, not fixed -- D286-D292

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D286 | the `orig_post_domain` key of four `targets_data` dicts -- `app/activitypub/util.py:3517` and `:3535` (this slice's region), `app/shared/post.py:577`, `app/models.py:2075` | **Not fixed -- `post.domain` is an ORM object placed into `Notification.targets`, a `db.JSON` column, so the flush raises `TypeError: Object of type Domain is not JSON serializable` for any post that already has a domain. Live and peer-reachable.** **The consequence is worse than a crash, and this is the half that was measured rather than assumed:** on the `util.py` path, `:3502`'s `db.session.commit()` has already flushed the new title, url and `POST_TYPE_IMAGE` and inserted a `File` row before the notification dict is built at `:3513-3518`, and no autoflush intervenes between `:3526` and the end of the function. So the rollback leaves **a half-applied `Update` plus an orphaned `File` row** that nothing points at -- not a clean failure. Lost in the same rollback: the domain reassignment (`:3544`), the `post_count` increment (`:3543`) and the notification itself. **The decisive register-not-fix reason is NOT "four sites, no correct spelling"; that reason was raised and withdrawn**, because `app/models.py:2087` is an in-tree precedent for a narrower `targets_data` (`{'gen': '0', 'post_id': post.id}`). **It is that NOTHING READS THE KEY.** `grep -rn orig_post_domain app/` returns exactly the four writers above and **zero readers**; every `targets` consumer in the codebase reads `post_id`, `comment_id`, `community_id` or `message_id` (`app/api/alpha/utils/user.py:705-812`, eleven subscripts, and `app/activitypub/util.py:2972`, a JSON-path filter on `comment_id`). With no consumer to arbitrate, choosing among `post.domain.name`, `post.domain_id` and dropping the key is a pure behaviour decision spanning three files and two subsystems. **REACHABILITY IS NOT UNIFORM, and the worst site is not in this function.** `util.py:3517`/`:3535` build the dict **before** `post.domain = new_domain` (`:3544`), so they serialise the post's **existing** domain -- peer-reachable via `app/activitypub/routes.py:1264`, `:2330`, `:2345`, but **conditional on the post already having had one**. `app/shared/post.py:577` is **worse**: `post.domain = domain` is at `:570`, **above** the dict at `:573-579`, so the value is never `None` when the dict is built -- the crash is **unconditional on prior domain state** and reached by a **local user editing a post**, not by a peer. Its two loops narrow it further, in opposite directions: the mod loop cannot reach the dict at all (**D287**), so `:577` is reachable only through the admin loop at `:590-598`; and `:568-569` raises for a **banned** domain before the dict, so it is the **notifying, non-banned** domain case that crashes. `app/models.py:2075` is **latent**: `post.domain` is unassigned above it, so a fresh post serialises `None` and nothing raises. | not fixed, registered only | measured, not reasoned: two characterisation tests in `tests/test_ap_update_post_tails.py` assert `pytest.raises(StatementError)` on a notification-producing post with a seeded `post.domain_id`, one per writer -- and **the second was proved necessary rather than assumed**, because the mod-arm pin ran with `notify_admins` unset, so `:3528`'s guard was `False` and `:3535` survived it. Both mutants (`post.domain` -> `post.domain_id` at each site) are now sole kills; under the mutant the id serialises fine and nothing raises. The half-applied-`Update` trace was re-derived independently step by step: `:3502` confirmed the first commit reached (the three earlier commits at `:3308`, `:3352`/`:3366` and `:3400` are in branches that return or do not apply to a `Page` `Update`), no autoflush between the dict and the end (`moderators()` queries before the add, `post`'s expired attributes reload at `:3515-3516`, `post.cross_posts` is NULL for factory posts so `:3548` never queries), and `make_image_sizes_async` stops at `:1759`'s status check and persists nothing. The four writers and zero readers re-grepped at this commit |
| D287 | `edit_post`'s moderator notification loop (`app/shared/post.py:580-589`), the call at `:582` | **Not fixed -- `community_member.is_local()` is a method `CommunityMember` DOES NOT DEFINE, so the loop raises `AttributeError` for ANY moderator.** `CommunityMember` (`app/models.py:3499-3513`) holds eight `db.Column`s, one `relationship` and one `__table_args__` and **no `def` of any kind**; the next class begins at `:3516`. The six `is_local` definitions in the file are at `app/models.py:795`, `:1251`, `:1848`, `:3201`, `:3767` and `:4237`, and every one belongs to another class -- the `:3767` one to `Report`, which starts at `:3741`. **Two consequences, and the second is why this is registered here rather than merely noted.** (1) A local post edited onto a `notify_mods` domain raises before any notification is built, so **no moderator is ever notified from this path** -- the loop's entire purpose fails, silently in the sense that it fails the same way every time and has presumably always done so. (2) It **narrows D286**: because the mod loop cannot reach the `targets_data` dict, `app/shared/post.py:577` is reachable only through the **admin** loop at `:590-598`, which corrects an earlier and broader severity claim. **Found while polishing a sentence, not while testing**: a reviewer's proposed narrowing *assumed* the call gates the mod row, and the implementer checked the class rather than accepting the mechanism. Not fixed because the repair is a behaviour choice, not a misspelling: `CommunityMember` has a `user` relationship (`app/models.py:3509`), so `community_member.user.is_local()` is *available*, but whether remote moderators should be notified is exactly the question **D288** records the codebase as disagreeing with itself about. **Outside this slice's file.** | not fixed, registered only | reading-level, verified **three times independently** -- by the implementer that found it, by the controller, and by a re-reviewer that read `CommunityMember`'s full body from raw source rather than through tooling and confirmed it contains no `def`. The six `is_local` definitions located by `grep -n 'def is_local'` and each attributed to its class by an unfiltered `^class ` scan. Not covered: no test in `app/shared/post.py`'s suite drives a local post edit onto a `notify_mods` domain with a moderator seeded |
| D288 | `update_post_from_activity`'s moderator notification loop (`app/activitypub/util.py:3520-3527`) against `edit_post`'s (`app/shared/post.py:580-589`) | **Not fixed -- the two copies of one loop DISAGREE about whether remote moderators are notified, and neither can be called the correct spelling.** `util.py:3520-3527` iterates `post.community.moderators()` and builds a `Notification` for every row with **no gate of any kind**. `shared/post.py:580-589` wraps the same body in `if community_member.is_local():`. So on the peer-`Update` path a remote moderator gets a `Notification` row they can never see, and on the local-edit path they do not -- except that the local path's gate is the broken call of **D287**, so what it actually does is raise. **The pair therefore offers no spelling to copy, which is the register test**: one copy notifies everyone, the other notifies nobody by accident, and picking between "notify remote moderators" and "do not" is a product decision about what a `Notification` row for an unreachable user is for. A third copy of the same loop exists at `app/models.py:2077-2085` and is **ungated**, matching `util.py` -- so the vote is 2-1 for ungated, but the one dissenter is the only one written with an explicit locality opinion. **This is the shape sub-project 14 named for guards and it is here for a whole loop**: two copies of one behaviour drifting apart with no test on either side to notice. **Outside this slice's file on one side.** | not fixed, registered only | reading-level, verified against source at this commit by two agents independently, each reading both loops side by side rather than inferring the second from the first; the third copy at `app/models.py:2077-2085` located by grepping for the `Notification(title='Suspicious content'` construction rather than for the words used to describe it. Covered on the `util.py` side only -- `tests/test_ap_update_post_tails.py` pins the ungated loop's row count and `already_notified` de-duplication, which pins what the loop does and says nothing about whether it should |
| D289 | `update_post_from_activity`'s attachment/url-change thumbnail call (`app/activitypub/util.py:3504`) against the `Event` block's identical call (`:3397`) | **Not fixed -- `make_image_sizes(image.id, 170, 512, 'posts')` at `:3504` runs UNGATED, while the same call at `:3397`, in the same function, sits inside `if get_setting('cache_remote_images_locally', True):` (`:3396`). Register with the weight it deserves: a peer `Update` alone is sufficient to make the instance FETCH AND RESIZE AN ARBITRARY REMOTE URL WITH CACHING DISABLED** -- a bandwidth and privacy consequence of a peer-controlled string, on an install whose administrator has explicitly turned the feature off. The setting is genuinely user-facing: declared at `app/admin/forms.py:81`, rendered at `app/templates/admin/misc.html:67`, written at `app/admin/routes.py:329` and read with default `True` at `:369`. **Three grounds for registering rather than fixing, and the third is what separates this from D285.** **(1) Two candidate correct spellings exist in-tree and DISAGREE.** The nearest analogue for a *post thumbnail* is the create path at `app/models.py:2265`, which gates on the flag **and** excludes video posts **and** picks dimensions by post type; `:3396`'s one-conjunct gate is the **event banner**'s spelling. Copying either would produce a third variant rather than reconcile two. **(2) `:3504` is not an isolated outlier.** In `app/activitypub/util.py` there are fourteen `make_image_sizes` call sites: **five gated** (`:765`, `:767`, `:1304`, `:1306`, `:3397`) and **nine ungated** (`:937`, `:939`, `:1111`, `:1113`, `:1497`, `:1499`, `:1689`, `:1691`, `:3504`), with two more ungated in `app/api/alpha/utils/user.py` (`:455`, `:474`). Which sites the flag governs is an unstated product decision, not an oversight at one line. **(3) Closing it CHANGES BEHAVIOUR FOR A LIVE INSTALL**: an administrator with the flag off today *does* get remote post thumbnails cached on this path, and a fix takes that away. D285's repair was behaviour-preserving for every shape but the one it fixed; this one is not. | not fixed, registered only | reading-level plus enumeration, re-derived at this commit rather than copied: every `make_image_sizes` call in `app/activitypub/util.py` located by grep and each classified by **reading its enclosing condition**, not by matching the call line -- `:936`, `:938`, `:1110`, `:1112`, `:1496`, `:1498`, `:1688`, `:1690` read to confirm the guard is a bare id/changed test with no `get_setting`, and `:764`, `:766`, `:1303`, `:1305`, `:3396` read to confirm the flag. `:3504`'s path traced to establish there is no enclosing gate: it sits inside `if image:` (`:3500`), under `if new_url:`, under `old_url != new_url`, with no setting check anywhere between. Covered: `tests/test_ap_update_post_tails.py` reaches `:3504` through a bodiless-404 image route, which is the file's chosen image boundary precisely because technique (2) -- turning the flag off -- cannot serve this cluster |
| D290 | `update_post_from_activity`'s poll `endTime` (`app/activitypub/util.py:3338`), against the `Event` block's `datetime.fromisoformat` twin (`:3376`; its `startTime` sibling is `:3375`) | **Not fixed -- the raw peer string is assigned straight into `Poll.end_poll`, a naive `db.DateTime` column, so a NON-UTC OFFSET IS DISCARDED RATHER THAN CONVERTED, and a malformed value raises `DataError` out of the function.** Measured against the running stack: `Poll.end_poll` is `db.Column(db.DateTime)` (`app/models.py:3782`) -> `timestamp without time zone`; `psycopg2`'s `adapt()` of a **string** yields a bare quoted literal, so the **column type** drives the cast; and `SELECT '2027-01-01T12:00:00+00:00'::timestamp` and `SELECT '2027-01-01T12:00:00+05:00'::timestamp` both return **12:00:00**. So a poll deadline sent from a `+05:00` peer is stored five hours late, silently, on a peer-reachable path. A malformed value gives SQLSTATE 22007 -> `InvalidDatetimeFormat` -> `sqlalchemy.exc.DataError`, which **escapes**, and the handler survey is stated precisely because a loose version of it would be wrong: the function contains four `try`/`except` blocks, and the three below `:3273` all sit inside the `Video` arm -- two `httpx`-scoped (`:3280-3282`, `:3284-3286`) and one bare (`:3289-3291`, around the `.json()` parse) -- an arm that commits and returns at `:3308-3309` and is therefore never on the poll block's path, the `Question` dispatch beginning at `:3311`. The nearest handler that could catch anything here is the `(ValueError, TypeError)` at `:3267-3269`, which is **above** `:3273` and out of scope, so nothing wraps the poll block's own commit at `:3352`. **THE TWO ARE NOT TWINS IN OUTCOME, AND THAT CORRECTION IS THE ENTRY.** The first report filed `:3338` and `:3376` as one defect wearing two spellings. Measured: `adapt(datetime.fromisoformat('...+05:00')).getquoted()` yields a `::timestamptz`-tagged literal -- psycopg2 **tags aware datetimes** -- and inserting that into a `timestamp` column **converts** by session `TimeZone`, giving 07:00:00. **So `fromisoformat` preserves the instant and only `:3338` corrupts.** The codebase's own precedent agrees: `parse_ban_expiry` (`app/activitypub/util.py:2402-2430`) writes an aware datetime into `User.banned_until`, also a naive `DateTime`, and this register already records that behaviour -- see the **`parse_ban_expiry` extraction described in D91's resolution**, whose test paragraph names "non-UTC offsets preserved as the same instant" as one of the twenty cases `tests/test_activitypub_ban_expiry.py` pins. **Caveat carried, because it bounds the claim:** the conversion target is the *server's* session `TimeZone`, so the twin is "correct instant under a UTC server", not "timezone-safe in general". Not fixed because the register-not-fix ground survives the correction on its **second** reason alone: a bare `fromisoformat` still crashes on malformed input, the hardened form the codebase settled on is `parse_ban_expiry`, and choosing among three behaviours -- discard, convert, or parse-and-degrade -- is new behaviour. **The family is wider than the instance**: the discarded-offset problem belongs to every naive `DateTime` column that takes a peer timestamp, not to `Poll.end_poll` alone, and this entry registers the family. | not fixed, registered only | measured against the running stack by **three** agents independently -- `\d poll` for the column type, both `::timestamp` casts, `adapt()` on a string and on an aware datetime, `SELECT 'not-a-date'::timestamp` for the SQLSTATE, and `SHOW TimeZone` = `Etc/UTC`. **And then confirmed INSIDE the function rather than at the psycopg2 layer**, which is the stronger evidence: `test_the_end_time_string_is_cast_by_postgres_to_a_naive_datetime` sends a second `Update` at `+05:00` -- the same wall clock, a genuinely different instant -- and asserts the **same** stored value, and the mutation replacing `:3338` with `datetime.fromisoformat(...)` is a **sole kill** through it, failing on that second assertion only (07:00 != 12:00). The second `Update` is load-bearing and the docstring says why: `+00:00` alone cannot distinguish "the offset was discarded" from "the value was converted to UTC", because both give 12:00. The slice's largest finding is therefore a committed regression witness rather than a deleted probe. Not covered: no test sends a malformed `endTime`, so the `DataError` half is established by measurement at the database rather than through the function |
| D291 | `update_post_from_activity`'s poll vote counting (`app/activitypub/util.py:3331`) and totals write (`:3365`) | **Not fixed -- `total_vote_count += vote['replies']['totalItems']` accumulates with NO sign check and NO type check, and `choice.num_votes = vote['replies']['totalItems']` writes the same unchecked value. Two consequences from one missing check.** **(1) A peer can route a vote-bearing `Update` into the DESTRUCTIVE Edit path with cancelling totals.** `:3333` is `if total_vote_count == 0:  # Edit, not a totals update`, so a document whose `totalItems` are `-3` and `+3` sums to zero, takes the Edit branch, and executes the two `DELETE`s at `:3341-3343` -- discarding every `poll_choice_vote` and `poll_choice` row for the post and recreating the choices from the peer's list. The comment on `:3333` states the intended discriminator, and a sign check is what would make it true. **(2) A negative `num_votes` reaches a division.** `:3365` writes the raw value, and `PollChoice.percentage` (`app/models.py:3828-3829`) is `math.floor(self.num_votes / poll_total_votes * 100)`. **The `ZeroDivisionError` is genuinely reachable, and that was established rather than asserted**: `app/templates/post/_post_full.html:271` guards `percentage` only when `poll_finished`, so the `has_voted` branch at `:284-285` calls `percentage(poll_total_votes)` on an **unfinished** poll whose totals sum to zero. **A third consequence, measured while deciding this entry's family membership: `totalItems` is not type-checked either.** `:3328`'s guard is a membership test, so `0 += "3"`, `+= None`, `+= ['a']` and `+= {}` all raise `TypeError: unsupported operand type(s)` at `:3331`, out of the function; `+= 3.5` does **not** raise and flows on to `choice.num_votes = 3.5` into an `Integer` column. Not fixed because the repair chooses behaviour at three points at once -- reject the document, clamp to zero, or skip the entry -- and because the *routing* consequence means a sign check changes which branch an existing shape takes, which is not a guard addition but a dispatch change. | not fixed, registered only | measured: the routing consequence is a live, committed witness -- the mutation deleting the Edit path's `return` -- at `:3353` at this commit, `:3351` when the mutation was run, before the two lines `2ff71ebf` inserted above it -- initially **survived**, and killing it required an `Update` whose totals sum to zero with non-zero components (`-3`/`+3`), so the test that exists leans on `:3331` having no sign check and demonstrates the route. The `ZeroDivisionError` reachability was found by a reviewer reading the template rather than by the implementer, and `:271` and `:284-285` are read at this commit. The `+=` type behaviour measured in a bare interpreter over six values (`'3'`, `['a']`, `None`, `3.5`, `True`, `{}`) rather than reasoned. Not covered on the `percentage` half: no test in this file renders a poll |
| D292 | `Post.new`'s poll choice loop (`app/models.py:2205-2209`), the create-path sibling of **D284** | **Not fixed -- `choice_ap['name']` is read unguarded, the identical defect D284 fixed in the two update-path loops, in the identical idiom.** `for choice_ap in request_json['object']['oneOf' if mode == 'single' else 'anyOf']:` then `PollChoice(post_id=post.id, choice_text=choice_ap['name'], sort_order=i)` -- no membership check, so a `Create` carrying a nameless choice raises `KeyError: 'name'`. **This is the campaign's recurring shape at a fourth generation**: sub-project 14 fixed a guard on the update side and left the create side carrying it (D261 records the same for a content gate); here sub-project 17 fixes the update side and this entry names the create side **in the same change that creates the asymmetry**, which is the whole point of registering it now rather than discovering it in a later slice. **Not fixed here on scope**: `app/models.py` is not this slice's file, the loop has no test coverage in this sub-project's suite, and the fix -- while mechanically the same one-line `continue` -- must be argued against the create path's own severity, where the surrounding `Poll` row is committed at `:2210` and the failure mode differs from the update path's rolled-back `DELETE`s. **The reason it is a register entry and not a TODO: `2ff71ebf`'s own commit body cites `app/models.py:2205-2209` as the sibling idiom it copied its `i += 1` placement from**, so the next reader of that commit reaches this loop and needs to know it was seen and left. | not fixed, registered only | reading-level, verified against source at this commit: the loop read in full at `app/models.py:2200-2210`, the subscript at `:2207` confirmed unguarded, and the `mode` ternary at `:2206` confirmed to select the peer's own list. Function attribution taken from an unfiltered `^def `/`^class ` scan rather than from the loop's indentation. **Not covered anywhere, and that was measured rather than left as a disclaimer**: grepping `tests/*.py` for the two poll-list keys `oneOf` and `anyOf` returns exactly two files -- `tests/conftest.py` and this slice's own `tests/test_ap_update_post_tails.py`, which drives the UPDATE path -- so **no test in the repository sends a poll `Create` through `Post.new` at all**, and this loop is uncovered rather than covered-but-unpinned. (There is no `tests/test_ap_create_post.py`; the create-path files are `tests/test_ap_create_reply.py` and `tests/test_ap_create_resolved_object.py`, and `create_post` itself is covered by `tests/test_ap_notify_post.py`) |

### 3. Four findings that are not production defects -- D293-D296

| # | function | defect | status | evidence |
|---|---|---|---|---|
| D293 | `update_post_from_activity`'s **head** (`app/activitypub/util.py:3183-3272`) -- sub-project 14's region, which that sub-project closed | **Not a defect in this slice -- THREE items inside a region a previous sub-project took to zero uncovered statements, none of which its own success criterion could have caught. Registered together because they share a cause.** **(1) The `author_user_name` ternary at `:3247`** -- `author.ap_id if author.ap_id else author.user_name`, inside the post-mention `targets_data` dict -- is **untested at BOTH arms**, the only ternary in the function with no test at all. The three tests that create that `Notification` (`tests/test_ap_update_pair.py`, the `post_mention` tests) assert subtype, url, the unread count and a row count, and `grep -rn author_user_name tests/` returns hits in **three** files and **none of them is `tests/test_ap_update_pair.py`**, which is the file covering this function: `tests/test_ap_create_reply.py` (nine hits, including live assertions on `notification.targets['author_user_name']` whose docstrings explicitly pin the *else* arm of the identical idiom), `tests/test_ap_notify_post.py` (eleven), and this slice's own `tests/test_ap_update_post_tails.py` (one). **All of them are other functions' copies of the idiom; not one reaches `:3247`**, and the file that would have to is the one with zero hits -- which is why the sibling coverage is evidence FOR the gap rather than against it. **Fact 87 is exactly why sub-project 14's criterion could not have detected it: coverage.py emits no arc for a conditional expression, so 100% statements and 100% branches are consistent with an unexercised arm.** **(2) and (3) The two residual branch arms.** `[3223, 3225]` and `[3263, 3261]` are the **only** missing branches in the whole function after this slice: the `False` sides of `if hashtag:` (`:3223`) and `if flair:` (`:3263`), i.e. the "lookup returned `None`" arms of `find_hashtag_or_create` and `find_flair_or_create`. Both are above `:3273` and therefore in sub-project 14's head, not this slice's tails. **Why they are grouped with the ternary rather than filed as an ordinary coverage gap:** all three are places where a *statement* figure of 100% is compatible with an unexercised path, and all three sat unnoticed through a sub-project that measured itself and passed. **What this does NOT say:** none of the three is asserted to be a defect. The two branch arms are cheap fixtures for whoever reopens that region, and the ternary's arms are display-name fallbacks whose mutants store a different string, not a crash -- fact 87(b)'s "cosmetic" category. **The finding is about the criterion, not the code.** | not a defect; gap recorded in a closed region | measured for the two branch arms -- the controller's post-slice coverage session reports `update_post_from_activity` with **zero** missing statements and exactly these two missing branches -- and read for the ternary: enumerated by an **AST walk** (`ast.parse` -> `IfExp` inside the `FunctionDef`, extent from `end_lineno`), which returns eight `IfExp` in the function at `:3183`, `:3232`, `:3247`, `:3266`, `:3268`, `:3418`, `:3460`, `:3509`, the split falling exactly at `:3273` -- **five above it** in sub-project 14's head (`:3183`, `:3232`, `:3247`, `:3266`, `:3268`) and **three below** in this slice's tails (`:3418`, `:3460`, `:3509`). Reproduced exactly by an independent AST walk at review. `:3223`, `:3225`, `:3261` and `:3263` re-read at this commit to confirm the two arcs are the guards named |
| D294 | `update_post_from_activity`'s old-domain lookup (`app/activitypub/util.py:3509`) | **Not a defect -- an EQUIVALENT MUTANT with no catalogued cause, and the specimen that adds a seventh cause to `tests/README.md`'s fact 75.** `old_domain = domain_from_url(old_url) if old_url else None` is redundant with the callee's own guard: `domain_from_url` (`app/utils.py:1561`) opens `if not url: ... return None` (`:1562-1568`) with no side effect before that return, so collapsing the `else` arm -- making it a bare `domain_from_url(old_url)` -- computes the same value **for all inputs**, not merely for all fixtures. **The taxonomy point is the entry.** **fact 75's own scope sentences** scope causes 1-5 to a **clause** and cause 6 to a **statement**; an *arm of a conditional expression* is neither. Cause 3 (subsumption) is defined over conjuncts and needs a later one implying an earlier; cause 4(a) needs a body writing back what the guard asserts; cause 4(b) needs the condition falsified before the guard runs, and `old_url` genuinely can be falsy here; cause 6 needs code running **after** the mutated statement, where this callee's guard runs **instead of** the collapsed arm. **The implementer declined to force-fit and proposed a seventh cause rather than editing the facts file itself**, which is the correct division of labour and is why the proposal arrived with its discriminators already worked out. **This register round accepted it as cause 7 and recorded that it stands on ONE instance where cause 6 stood on two** -- listed anyway, because the alternative on meeting this shape is a mis-filed 4(a) or a fake kill, which is precisely what fact 75 exists to prevent. **The arm is still worth covering** even though the mutant is unkillable: a later change to `domain_from_url`'s guard would make the two arms diverge, and only a test exercising the falsy input would notice. **And note where this cause lives:** fact 87 says coverage emits no arc for a ternary, so an unkilled expression arm is invisible to both figures and is only ever found by an AST enumeration followed by a mutation -- which is how this one was found. | not a defect, equivalence recorded; fact 75 gains cause 7 | reading-level plus source-verified, and the equivalence upheld independently at review: `app/utils.py:1562-1568` read in full and confirmed to be `if not url:` -> comment -> `return None` with no statement between the test and the return, so the equivalence holds for **all inputs** rather than for the fixtures present. The taxonomy exclusion checked cause by cause against `tests/README.md`'s own scope sentences rather than by impression. Covered: the `if` arm has an existing killer and the `else` arm is cited in the test file's cluster banner rather than duplicated |
| D295 | `Site.admins()` (`app/models.py:3995-4000`) -- a **plan** claim corrected, and a reachability verdict future fixtures need | **Not a defect -- `Site.admins()` returns `[]` for a roleless `User.id == 1`, and the plan for this sub-project asserts the opposite THREE times.** The plan reads, at `docs/superpowers/plans/2026-09-04-coverage-update-tails-17.md:97-101`, `:707-712` and `:904-906`, that the method "joins `user_role` for `ROLE_ADMIN` **or** `User.id == 1`" and therefore that "user 1 is an admin by that `or_` clause without any role row". **False.** The query is `db.session.query(User).filter_by(deleted=False, banned=False).join(user_role).filter(or_(user_role.c.role_id == ROLE_ADMIN, User.id == 1))` -- and `.join(user_role)` carries no `isouter=True`, so it is an **INNER** join: a user with **no** `user_role` row is eliminated from the result set **before** the `or_` is ever evaluated, and `User.id == 1` cannot rescue a row the join already dropped. **Which arm the fixtures take was established rather than assumed**: `Site.admins()` reads `g.admin_ids` when present (`:3996-3997`), `tests/conftest.py:137` is `g.__dict__.clear()`, and nothing in the suite sets `admin_ids` -- so **every** test takes the join arm, and each admin fixture must be given a role row explicitly. **One tightening in the tests is load-bearing and is recorded so it is not simplified away:** the fixtures grant an ordinary non-staff role rather than `ROLE_STAFF`, because `Site.staff()` (`app/models.py:4002-4005`) is the same join narrowed to `ROLE_STAFF` **without** the id disjunct -- so a user 1 holding `ROLE_STAFF` would let an `admins()` -> `staff()` mutant explain the same row, and the mutation would survive. **Every original line of the plan is left exactly as written and the correction lives here**, which is this file's convention for a dated planning record: the plan was a forecast, the forecast was wrong, and rewriting it would delete the evidence that "verify rather than inherit" earned its place in the constraints. **The pointer is made TWO-WAY rather than one-way, on the controller's Ruling N, in the shape sub-project 16 used**: the plan carries a dated annotation appended after its last original line -- appended, because any insertion above would shift the very line numbers this cell cites -- naming the three claims, giving the corrected reading and directing the reader here. A reader who opens the plan first is sent to this entry; a reader who opens this entry first is told the plan is unrevised. The append was verified to be exactly that: 4 insertions, 0 deletions, every one of the plan's **971** original lines byte-identical and in order (`971` is `wc -l`; an earlier draft of this clause said 972, which was `split('\n')` counting the trailing empty element -- the verification itself was sound, only the number was a counting artefact). **What makes this worth a number rather than a process note: the false claim was written by the controller and repeated into two further artefacts as an established fact, and the standing instruction to verify it from source sits in the same paragraph.** The instruction is what caught it. | not a defect; the plan's claim corrected, the plan unrevised | reading-level, verified **three times independently** -- by the implementer that was told the claim and checked it anyway, by the reviewer, and by the controller. `app/models.py:3995-4000` read in full at this commit, the absence of `isouter=True` confirmed by reading the `.join(` call rather than by grepping for the flag, and `Site.staff()` at `:4002-4005` read side by side to establish the ordinary-role tightening. `tests/conftest.py:137` read to confirm `g` is cleared. Covered: every admin-arm test in `tests/test_ap_update_post_tails.py` names in its docstring which disjunct its fixture satisfies |
| D296 | `tests/factories.py:769` -- a **test-suite** finding, out of every task's slice | **Not a production defect -- `make_poll`'s docstring cites `Poll.post_id` at `app/models.py:3745`; it is at `:3781`.** The claim the citation supports is correct: `post_id` **is** `Poll`'s primary key (`post_id = db.Column(db.Integer, db.ForeignKey('post.id'), primary_key=True)`), so a post has at most one poll and the identity map returns the same object for the same post, which is what makes the arm's `.get()` lookup work. Only the pointer is wrong. **Recorded here rather than fixed for two reasons, and the second is the transferable one.** `tests/factories.py` belonged to no task in this sub-project, and this campaign's rule is that a task does not edit a file outside its slice to tidy a citation. And **it is not established whether this cell was wrong when written or has since drifted** -- `app/models.py` has grown by hundreds of lines across the campaign, so 36 lines of drift is entirely ordinary -- which decides the repair: a citation that was right when written is re-read before reuse and left alone, while one that was wrong when written is fixed in place. **Whoever owns `tests/factories.py` next should determine which it is before editing**, and this entry exists so that determination has somewhere to start. | not a production defect; recorded for the owner of `tests/factories.py` | measured at this commit: `app/models.py:3781` read directly and confirmed to be `Poll.post_id`, with `class Poll(db.Model)` at `:3780`; `tests/factories.py:764-775` read in full to confirm the cited claim itself is true and only the coordinate is stale. Found by the task that read `make_poll` while writing poll fixtures, which correctly left the file alone and reported it |

**Next free number: D297.** D284-D296 were taken by sub-project 17 -- D284 and
D285 the two defects fixed, D286-D292 seven registrations, and D293-D296 four
findings that are not production defects. **No earlier entry was renumbered or
moved by this sub-project, and exactly ONE was edited: D283's parenthetical
split of its nine `author` hits, corrected in place because it was wrong when
written rather than drifted** -- sub-project 16 parked that residual for this
round to verify before repairing, and the verification is recorded in the cell
itself. **The second parked residual, the distinctness guard in
`tests/test_ap_notify_post.py`, IS closed -- and closing it disproved the
premise it was filed under.** The residual asked for `post.instance_id` to join
the guard's set with the length adjusted. **It cannot join it**: measured, the
five-element assertion fails `assert 4 == 5 / where 4 = len({1, 2, 3, 4})`,
because `post.instance_id` equals `community.id` **by construction** -- the
fixture creates the peer `Instance` first so it takes id 1, `make_community`
hardcodes `instance_id=1`, and `make_post` copies the author's `instance_id`
(`tests/factories.py:337`), which is that same instance. Separating them means
restructuring an id-occupation pattern every test in that file depends on. So
the guard keeps its four-element set and gains two explicit lines --
`post.instance_id == community.id` and `post.instance_id != post.id` -- plus a
comment stating that the `'post_id': post.id` -> `post.instance_id` mutant is
killed by the **second** of those rather than by an independently-witnessed id,
i.e. by exactly the property that kills the `post.community_id` mutant.
**Measured, not asserted**: the mutation applied to
`app/activitypub/util.py:2829` gives 1 failed / 33 passed, a sole kill by
`test_a_subscriber_to_the_author_is_notified`, with `app/` restored and
md5-verified against HEAD afterwards; the file is green at 34 passed.
**The lesson is fact 89's, sharpened: `RESTART IDENTITY` collisions are not all
fixable, and a distinctness guard that cannot be made true is closed by naming
the collision and pinning the property that actually does the work -- not by
asserting a length the fixture cannot produce.** The
unguarded-peer-input family index in sub-project 16's subsection 5 above is
extended by this sub-project, and the extension is described in the paragraph
below. **What was actually written into that subsection, stated exactly, because
an earlier draft of this sentence claimed the index was "extended in place" when
it carried no hunk at all:** ONE marked sentence is appended to its arithmetic
paragraph's opening, saying that the paragraph's range is superseded, giving the
re-derived figures and pointing here. **No row of its table, no entry of its
rejection list and no line of its membership test was edited, moved or
renumbered.** If you take D297, say so here in the change that
takes it.

**THE FAMILY INDEX'S PARTITION IS RE-DERIVED, NOT INCREMENTED, AND ITS SHAPE
CHANGES.** Sub-project 16's index (subsection 5 of its section) declares itself
complete over "every register entry whose function is one of `create_post`,
`create_post_reply`, `update_post_from_activity`,
`update_post_reply_from_activity`, `notify_about_post_task`", and its arithmetic
read **17 members + 31 rejections = 48 = D236 through D283 inclusive**. That
partition was **re-derived here rather than trusted**: the seventeen members
re-counted off the index's own table (D236, D237, D238, D245, D246, D247, D255,
D256, D257, D258, D259, D261, D264, D270, D271, D272, D273) and the thirty-one
rejections re-counted off its rejection paragraph (D240, D243, D239, D260, D251,
D263, D241, D242, D244, D248, D249, D250, D252, D253, D254, D262, D265, D266,
D267, D268, D269, and D274-D283) -- 17 + 31 = 48, and D236-D283 is 48 numbers,
with no overlap, no gap and nothing out of range. **The prior partition holds.**

**This sub-project adds four members and five rejections, and for the first time
the range is NOT wholly inside the declared scope -- which is a change to the
claim's shape, not only to its arithmetic.** Four of this section's thirteen
entries are **not** in a function the index scopes to, so "D236 through D296
inclusive" would be a false completeness claim if written the way the previous
one was. Stated so it is arithmetically checkable:

- **Four new MEMBERS**, each satisfying the index's own membership test -- a
  peer-supplied value read without the guard that read needs, so a document a
  peer is free to send raises out of the function. **D284** (`vote['name']` in
  two loops, `KeyError`, **fixed** at `2ff71ebf` -- listed as fixed, which the
  index already does for D236, D237, D238, D257 and D261). **D285**
  (`request_json['object']['image']['url']`, `KeyError`, **fixed** at
  `30a9dcec`, with the non-dict residue still open at `:3391` and `:3487`).
  **D290** (`endTime`, `DataError`) -- **admitted on D257's precedent**, where
  the guard is present but its domain is one type too narrow: `:3336-3337`
  checks membership and nothing checks the value. **D291**
  (`vote['replies']['totalItems']`, `TypeError`) -- the same shape, membership
  checked at `:3328` and type not.
- **Two things D290 and D291 change about how the family must be swept**, and
  they are worth more than the two rows. **(a) D290 is the family's first
  member whose exception is raised at FLUSH rather than at the read.** The index
  already warns that grepping the family for `KeyError` finds eight of
  seventeen; D290 adds `sqlalchemy.exc.DataError` to the spread and, worse,
  raises it several statements away from the unguarded read, at `:3352`'s
  commit. **A sweep that looks for a raise at the read site cannot see it.**
  **(b) D291's crash was found while deciding its membership, not before.** The
  entry was raised for the *sign* of `totalItems`, which raises nothing; the
  `TypeError` on a string, a list, a null or a dict was measured in a bare
  interpreter as part of applying the membership test. **Applying the test is
  itself an investigation, which is fact 94's point arriving from the other
  side.**
- **Five new REJECTIONS**, in scope and out of the family, with the reason so
  the next sweep does not re-open them. **D286** -- the crash is real, but the
  unserialisable value is `post.domain`, an **ORM object the function itself
  assigned**, not a peer-supplied read; that is D274's distinction (a value from
  a database column rather than from a peer document) on a third source.
  **D288** -- a locality asymmetry between two loops, with no read and no
  exception. **D289** -- an ungated resource fetch; the peer-controlled string
  is read successfully and the consequence is bandwidth and privacy, not a
  raise. **D293** -- a coverage-criterion finding about a closed region, with no
  peer read in it. **D294** -- an equivalent mutant, an identity between two
  spellings of one lookup.
- **Four entries OUTSIDE the index's declared scope**, named explicitly so the
  arithmetic below closes. **D287** (`edit_post`, `app/shared/post.py`) and
  **D295** (`Site.admins()`, `app/models.py`) are in neither of the five
  functions. **D292** is in `Post.new` (`app/models.py:2205-2209`) and **is the
  family's shape** -- an unguarded `choice_ap['name']` that raises `KeyError` --
  but its function is a **callee**, which is precisely the axis D35's note says
  a function-scoped sweep structurally cannot see; it is recorded here as a
  member-in-substance and out-of-scope-in-index, exactly as D218, D219 and D35
  are. **D296** is a finding about `tests/factories.py` and is not production
  code at all.

**The re-derived arithmetic: 21 members + 36 rejections = 57 in-scope entries,
plus 4 entries outside the declared scope (D287, D292, D295, D296), = 61 =
D236 through D296 inclusive.** Every number in the range is accounted for
exactly once, and the count was re-derived from the two lists rather than
incremented from 48 -- fact 94 applied to this paragraph's own arithmetic, which
is where a completeness claim is likeliest to be incremented without being
re-checked. **A future task should note that the "= the whole range" identity
the previous partition enjoyed is now broken and will stay broken**: the
campaign has begun registering entries in `app/shared/`, `app/models.py` and the
test suite, and the index's five-function scope no longer covers a contiguous
block of numbers. Say which property bounded your candidate set, not which range
it happened to occupy.

**Eight shapes carried forward into `tests/README.md`, four as new facts 96-99
and four as widenings of existing facts.** The new facts: a correction that only
**deletes** a false claim will be re-derived, because a deletion leaves nothing
on the page saying why the natural phrasing is wrong -- land the refutation in
the artefact being corrected, not only in the report that found it (**96**, the
sibling of fact 82 rather than a restatement: 82 is about a claim with existing
copies, this is the case with no copy at all); `db.session.expire(obj)` before
the assertions is what pins a `commit()`, because a deleted-commit mutant is
otherwise invisible to a read through the writing session under
`autoflush=False` -- a third case alongside facts 58 and 65, discriminated by
what the mutant did (**97**); a stale count in one row of a mutation table means
the table predates something, so **re-sweep it rather than patching the row**,
and watch especially for a sole -> multi status change, because other prose
leans on soleness (**98**); and inside a file you are editing, locate things by
**ordinal position** among named siblings rather than by absolute line number,
**re-derive every line number you copy out of a report or a review**, and run a
citation sweep in **two** passes, because a `file:line` pattern cannot see a bare
filename and a bare filename is the likeliest reference to be invented (**99**).
The widenings: **fact 75 gains a seventh cause**, scoped to an **arm of a
conditional expression** where its first five are scoped to a clause and its
sixth to a statement -- an arm whose sibling computes the same value because the
callee already guards (D294), recorded as standing on one instance where cause 6
stood on two; **fact 71 gains the worked five-shape table** it had been
predicting, showing that a membership guard in front of a subscript changes the
outcome for exactly two of four non-dict shapes and that which two depends on
whether the key name is a substring of the value (D285); **fact 74 gains two
corollaries**, that a regression spot-check must run through the test you
changed and that the "same commit" clause is the half that slips; and **fact 80
gains two more ways a mutation round damages the tree** -- never batch
mutations, because a batch that dies partway leaves no record of which mutant is
applied, and once a fix lands mid-slice the restore check must be against
**HEAD** rather than against the slice's base.

**(This sentence is written after counting: eight shapes, four new facts 96-99
and four widenings -- of facts 71, 74, 75 and 80. It was WRONG on its first
draft, which opened "Four shapes ... four as new facts and four as widenings",
4 + 4 stated as 4; caught by this round's own self-review before commit.
Sub-project 16's equivalent sentence was wrong twice the day it was written and
this one was wrong once, which is two sub-projects running. The arithmetic about
your own work is the arithmetic that gets skipped, and the cheap remedy is to
write the count LAST, from the file, rather than from the plan.)**

**ONE CITATION-VERIFICATION LESSON FROM THIS SECTION'S OWN REVIEW, AND IT IS
ABOUT THE SWEEP RATHER THAN THE ERROR.** This section's register round verified
its citations mechanically, by extracting them with a regex and checking each
against source -- and it still shipped a reference to
`tests/test_ap_create_post.py`, **a file that does not exist**, which the review
caught. Re-run afterwards, the cause is exact and not a lapse of care: the
extraction pattern required a **digit** after the filename, because it was built
to harvest `file:line` citations, so it never saw a reference written **without**
a line number. **Re-derived from the section as originally shipped (`9bfb8227`),
with the patterns stated so the figures can be reproduced:** counting
directory-prefixed paths -- `` `((?:app|tests|docs|migrations)/[\w./-]+\.(?:py|html|md|ini)) ``
-- the section carries **23** distinct file references, of which the `file:line`
pattern `` `([\w./]+\.(?:py|html|md|ini))[:`]?(\d+)(?:-(\d+))? `` catches
**15**, leaving **eight bare-only paths never checked**, the non-existent test
file among them. **The generalisable half: a filename with no line number is
the reference MOST likely to be invented, precisely because nothing forces the
writer to open the file, and it is the one a `file:line` sweep structurally
cannot see.** A citation sweep must therefore run **two** passes -- resolve every
`file:line` against its content, and resolve every bare path against
`git ls-files` -- and the second pass is the cheaper and the more often skipped.
Re-run here over all 25: every one resolves except the single deliberate negative
in D292's cell, which says the file does not exist. **The eight reproduces; the ratio first reported around it did not, and that is
the second finding.** This round first wrote "17 of 25", and the pair is not
reproducible even though both halves are individually derivable: the **17** came
from the `file:line` pattern, whose `[\w./]+` does not require a directory
prefix and so also matched two bare-basename citations (`util.py:3517`,
`shared/post.py:580-589`) that the other pattern excludes by construction, while
the **25** was the *post-fix* section under the dir-prefixed pattern. A reviewer
re-deriving it arrived at a third pair again. **A ratio whose numerator and
denominator come from different patterns over different revisions is not a
measurement**, and the correction is to state the pattern, state the revision,
take both halves from one run, and lead with the figure the argument rests on
rather than the ratio that dresses it. **This is fact 94 in miniature** -- the
sweep was applied to a candidate set the candidates could opt out of by omitting
a colon -- and both halves are now folded into fact 99(c).

**Two candidates were considered and DROPPED, recorded so they are not
re-proposed.** **(1)** A proposed harness fact that fact 74's "same commit"
clause "needs a gate, not vigilance" was dropped as a fact and folded into fact
74 as a corollary instead: it restates 74's existing rule and proposes no
mechanism, and a fact that says "this rule should be enforced" is not a fact.
**(2)** A dispatch instruction that a task which has stalled twice is handed to
a **fresh** implementer rather than resumed was dropped: it is controller
process, not harness knowledge, and `tests/README.md` is addressed to whoever is
writing a test. **And one clause was dropped from fact 96's own drafting
brief**: the brief proposed that fact 96's root cause is the `sed -n 'A,Bp'`
off-by-one. It is not -- that is **fact 95's** cause and is already recorded at
`tests/README.md` under fact 95. Fact 96's cause is different and is what the
ledger's own account describes: a fresh writer regenerating a wrong phrasing
because no refutation was on the page. **Registering what was found rather than
what was predicted applies to the brief that commissioned the register too.**

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

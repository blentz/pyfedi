# Sub-project 5a: the inbox dispatcher's preamble and its dispatch arms

`app/activitypub/routes.py`'s `process_inbox_request` — the preamble every
activity crosses, the arms that are genuinely dispatch, and the four vote
delegates those arms call. 114 statements of the function's 770, plus 83
statements of delegate: 197 in total, of which 5 are currently executed under
test, and all five are function-definition lines.

## Why this, and why now

Sub-project 4 covered the gate and stopped, correctly, at its own boundary. Task
8 Step 3 of that plan required the register to say precisely how far the tests
reached, and the answer it recorded was: **dispatch, not the handlers**. D21,
D24 and D35 remain reading-only for reachability because nothing has executed
the function on the other side of that dispatch.

`process_inbox_request` is that function. It is the largest single uncovered
unit in the repository — 770 statements against 1 executed — and every remote
activity PieFed accepts passes through its first 71 statements. The signing
lever built for sub-project 4 reaches its front door; nothing yet goes through
it.

## Why this is 5a and not sub-project 5

At 770 statements the dispatcher is roughly three times any sub-project this
campaign has completed. The campaign's own record is the argument against
taking it whole: slicing large functions across tasks has produced three
enumerations that were wrong on re-derivation in sub-project 3 alone, and those
were slices of far smaller functions. A plan long enough to cover 770 statements
is a plan too long to review honestly.

The split is by what a test must set up, not by line count:

| slice | statements | contents |
|---|---|---|
| **5a** | 197 | the preamble, the vote arms and their delegates, Flag, Move, QuoteRequest, the `except`/`finally` |
| 5b | 175 | Follow, Accept, Reject |
| 5c | 263 | Delete, Lock, Add, Remove, Block |
| 5d | 160 | Undo and its seven sub-types |
| separate | 58 | Create/Update, which is guard-dense ingest, not dispatch |

5a comes first because the preamble is a hard prerequisite for every other
slice. No test of any arm can be written without first driving actor resolution
and the Announce unwrap, so the preamble is this sub-project's shared lever in
the way the signing helper was sub-project 4's. Building it as a deliverable,
under test, is cheaper than four slices each hand-rolling it.

Create/Update is excluded deliberately. Its 58 statements are domain matching,
a `local_only` gate, two permission checks and a poll-vote path hidden inside a
`Note` — guard-dense ingest code, which is where this campaign has found most of
its defects. It earns its own spec rather than riding along as a tail on this
one.

## Scope

To 100% statement and branch coverage or a documented reason, with mutation
evidence per guard:

| lines | statements | unit |
|---|---|---|
| 839–934 | 71 | preamble: the session setup, actor resolution, the Announce unwrap, `core_activity`/`announced` |
| 1328–1343 | 12 | the four vote arms |
| 2387–2410 | 20 | `process_upvote` |
| 2413–2433 | 18 | `process_downvote` |
| 2436–2460 | 21 | `process_poll_vote` |
| 2463–2496 | 24 | `process_question_answer` |
| 1344–1355 | 8 | Flag |
| 1571–1589 | 14 | Move |
| 1880–1884 | 5 | QuoteRequest |
| 1885–1889 | 4 | the `except`/`finally` every arm unwinds through |

The preamble's span starts at the `def`, not at the first branch: the app
context, `get_task_session()`, `patch_db_session`, `saved_json`, `id` and
`actor_id` are all set up before the first decision, and every test of every
later slice runs through them.

The four vote delegates are in scope rather than doubled because each arm is
three statements — a test, a delegate call and a `return`. A slice whose entire
deliverable was those twelve statements would be a sub-project in name only, and
the delegates
are small, reachable and carry real guards (`can_upvote`, `instance_banned`,
`blocked_users`, `VOTE_QUOTA`, a moderator/admin/author triple, a Redis lock).

### Out of scope

Doubled and asserted at the call, never entered: `process_new_content` (86
statements), `process_chat` (71), `process_report`, `announce_activity_to_followers`,
`process_announce_of_uri`, `resolve_remote_post_from_search`. Each is either a
later slice or already covered elsewhere; `resolve_remote_post_from_search` was
sub-project 3's.

Follow, Accept, Reject, Create/Update, Delete, Lock, Add, Remove, Block, Undo
and their delegates: slices 5b–5d and the Create/Update spec.

Fixing anything this sub-project finds. Findings are registered; fixes are their
own change, as they have been since sub-project 2a.

## Architecture

### The entry point

The workhorse is a direct call — `process_inbox_request(activity, store_ap_json)`
inside an app context, with actors seeded as real rows so
`find_actor_or_create_cached` resolves off the database rather than the network.

This is not a contrivance for testing. It is the production DEBUG branch:
`routes.py:758-761` calls `process_inbox_request(request_json, store_ap_json)`
directly when `current_app.debug`, and `.delay(...)` otherwise. The function is
`@celery.task`-decorated, so the direct call is the task's own body.

The alternative — driving every arm end to end through a signed POST — was
rejected as the default. It re-runs the entire gate for every arm test, and a
failure is then ambiguous between the gate and the arm under test. It is kept
where it buys something, below.

### The seam suite

Three signed end-to-end tests through `signed_inbox_post` with `DEBUG=True`,
which let the gate call the dispatcher for real. They prove the gate reaches the
dispatcher, and that the resolved actor and `store_ap_json` arrive as the arms
assume.

This exists for one reason: it is what licenses the reachability language in the
register. The campaign's recurring correction is overstated reach. Without the
seam, this sub-project would earn that correction in reverse — arms proven,
entry assumed — and the register would have to say so. Three tests convert the
claim into a measured one.

The claim they license is narrow, and the register must keep it narrow: the gate
reaches these arms, for these three shapes. The arms 5b–5d will cover remain
unproven from the entry point.

### Fixtures

Reused unchanged: `signed_inbox_post` and `inbox_activity` from `tests/factories.py`
(sub-project 4, Task 1), the federation-peer fixture, `block_outbound_http`,
`redis_double`.

`redis_double` needs confirming rather than assuming. The findings doc records
that it covers `get_redis_connection`, not `redis_client` — a correction the
campaign has already had to make once. `process_question_answer` takes
`redis_client.lock(...)` through an inline import, so Task 1 establishes which
double applies before the votes file is written.

### File structure

| file | responsibility |
|---|---|
| `tests/test_inbox_dispatch_preamble.py` (create) | actor resolution, the Group branch, the refusals, the seam suite |
| `tests/test_inbox_dispatch_announce.py` (create) | the unwrap: string, list, `OrderedCollection`, the inner-actor walk, the banned check |
| `tests/test_inbox_dispatch_votes.py` (create) | the four arms and the four delegates |
| `tests/test_inbox_dispatch_misc.py` (create) | Flag, Move, QuoteRequest, the `except`/`finally` |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | the register |
| `coverage_floors.ini` (modify) | the raised floor |

Four files rather than one, split by what a test must set up: the preamble tests
need only a seeded actor, the Announce tests need an inner object and real
recursion, the vote tests need posts, replies, polls and a Redis double, and the
miscellaneous arms need reports and communities. Splitting by setup keeps each
file's fixtures honest — the same reasoning that gave sub-project 4 three files.

## What reading has already found

Registered during the sub-project, in the task that derives each unit's outcome
table. Listed here so the plan has targets and so a later reader can tell what
was predicted from what was discovered.

- **`routes.py:857`** — `actor_id = actor_id['id']` on a dict actor, unguarded.
  The same shape as D47, one function away.
- **`routes.py:878`** — `elif request_json['type'] == 'Update' and 'type' in request_json['object']`.
  If `object` is a string, `'type' in <str>` is a legal substring test, so a
  string object containing the letters "type" passes and the next line indexes
  it. This is the rule the findings doc states in one line: **a membership test
  is never a type test.** It explains D13's original miss and D30's surviving
  mutant, and here it is again.
- **`routes.py:911`** — `request_json['object']['orderedItems']`, unguarded on a
  peer-supplied `OrderedCollection`.
- **`routes.py:915`** — `request_json['object']['actor']`, unguarded.
- **`routes.py:859`** — `if actor_id and actor_id.startswith('https://s.rimu.geek.nz'): pass`,
  commented "just here to set breakpoints on, during testing. remove before
  commit". A shipped debug hook in the path every activity crosses.
- **`routes.py:901-907`** — the list arm recurses once per element with no
  bound, under a comment reading "an unlimited amount of objects at once".

None of these is fixed here. Each is a row, with severity argued from what the
tests actually executed rather than from reading.

## The quality bar

Unchanged from sub-project 0, and restated because two of its rules bear
directly on this code:

**A guard is tested on the whole domain it claims to reject**, not on the one
example that motivated it. D13's guard raised on the very input it existed to
catch and survived two reviews; the preamble's `'type' in request_json['object']`
is the same shape and must be probed with a string, not only with a dict.

**Each half of a compound guard is dropped separately and must die distinctly.**
The preamble is full of them — `if not community and not feed and not user`,
`if actor and isinstance(actor, User)`, `can_upvote(...) and not instance_banned(...)`.
A mutation that kills is evidence about the input chosen, not about the guard.

**The recursion is exercised, not doubled.** The list and `OrderedCollection`
arms call `process_inbox_request` on each element. Doubling the module-global to
test them means doubling the function under test, which produces a green test
proving nothing. The tests let the recursion run against seeded rows, with two
elements, asserting both were processed.

No `if TYPE_CHECKING`. No new inline imports.

## Verification

Measured baseline for `app/activitypub/routes.py`, at `a0bc3b22`: **310 of 1813
statements, 89 of 890 branches, 14.76 % blended, floor 14.**

Covering the 192 not-yet-executed statements and their branches projects to
roughly 24 %. The design
commits to raising the floor, not to that figure: the floor is set from the
measured result at the end, one point below it, exactly as sub-project 4 set 14
against 14.76.

Every task ends with the documented chain — suite, then coverage, then ratchet —
`&&`-chained so a red suite cannot reach the ratchet.

The final task measures the preamble and each delegate whole, reading
`executed_lines` and `missing_branches` against each span, and **explains every
gap**. An unexplained remainder fails that task; it does not pass with a note.

## Risks

**The task-session machinery under a direct call.** `process_inbox_request`
opens `current_app.app_context()` itself, takes a `get_task_session()`, runs
under `patch_db_session`, and closes the session in `finally`.
`tests/test_ap_refresh_community_profile.py` proves that pattern runs in this
harness, but against a different entry point that does not open its own app
context. Task 1 is a spike with an explicit stop condition, as sub-project 4's
signing lever was: if the session cannot be driven honestly, the sub-project
stops there and reports, rather than reaching for a contrivance that turns a
test green.

**The seam tests depend on `DEBUG=True` behaving as sub-project 4 measured.**
That work found there is no DEBUG split in `replay_inbox_request` where one
might be expected, and recorded it. The seam tests rest on the split that does
exist, at `routes.py:758-761`, which sub-project 4 covered directly.

**Two test runs against the same tmpfs database corrupt each other's results.**
One suite run at a time per worktree, as the process notes require.

**Scope creep into 5b.** The preamble sets `community`, `feed`, `user`,
`announced` and `core_activity` for arms this sub-project does not cover. Tests
that assert on those values for a Follow or a Block are testing 5b's arms through
5a's preamble, and the plan must refuse them: the preamble's contract is the
values it produces, verified at the point it produces them.

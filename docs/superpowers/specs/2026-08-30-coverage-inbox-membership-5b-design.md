# Sub-project 5b: the inbox dispatcher's membership handshake

`app/activitypub/routes.py`'s `Follow`, `Accept` and `Reject` arms inside
`process_inbox_request` — 175 statements, 3 of them executed, and those three are
only the `if core_activity['type'] == ...` guard lines that sub-project 5a's tests
fall through on their way elsewhere.

## Why this, and why now

5a covered the dispatcher's preamble, its dispatch arms and four delegates, and
raised the module from 14.76 % to 26.378 % blended. It deliberately stopped at
the arms that carry state: 5a's arms mostly delegate or refuse, while these three
write `CommunityMember`, `FeedMember`, `UserFollower`, `CommunityJoinRequest`,
`FeedJoinRequest` and `UserFollowRequest` rows, adjust subscription counters, and
send signed federation replies.

That makes this the first slice of the dispatcher where a defect corrupts
persistent membership state rather than dropping an activity. It is also where
5a's D60 — the inline/queued session divergence — stops being theoretical: the
Follow arm mixes `db.session` and the task-local `session` inside a single
logical write.

## Scope

To full statement and branch coverage or a documented reason, with mutation
evidence per guard:

| lines | statements | arm |
|---|---|---|
| 935–1073 | 80 | `Follow` — Community, Feed and User targets |
| 1075–1148 | 60 | `Accept` — the a.gup.pe string form and the `Follow`-object form, then three target branches |
| 1150–1191 | 35 | `Reject` — three target branches |

Measured at `92113827`: 3 executed, 172 missing.

### Doubled, not entered

`send_post_request` — the outbound Accept and Reject federation sends — and
`cache.delete_memoized`. The sends are doubled, but their **payloads are
asserted**: that JSON is the federation contract, and a test that only counted
calls would not notice the `object.id` echo or the actor/key pairing changing.

### Out of scope

The moderation arms (Delete, Lock, Add, Remove, Block) and `Undo`: slices 5c and
5d. Create/Update keeps its own spec. Everything 5a covered stays covered; this
slice adds to the same module and the same floor.

## The two fixes

This sub-project departs from the campaign's register-don't-fix rule for exactly
two defects, on the project owner's explicit instruction. Both are fixed
test-first, in their own commits, separate from every test-only commit.

The departure is bounded: **no other defect this slice finds is fixed here.**
Everything else is registered, as D1–D63 were.

### Fix 1 — the a.gup.pe Accept path cannot succeed

`routes.py:1077-1085`. An `Accept` whose `object` is a string (a.gup.pe sends the
follow request's ID rather than embedding the `Follow`) looks up the
`CommunityJoinRequest`, then does:

```python
user = session.query(User).get(join_request.user_id)
```

Control then reaches `if not requestor_user:` at :1091, which that branch never
assigns, so the path **always** logs `'Could not find recipient of Accept'` and
returns. The work it did is discarded every time.

The correct target is not inferred from style. The function documents its own
variable contract at :1076:

> `requestor_user` is the one two made the follow request originally while `user`
> is the one who sent the Accept

By that contract the join request's user IS `requestor_user`, and assigning it to
`user` is backwards twice over: it leaves `requestor_user` unset, and it clobbers
the actor who sent the Accept — which the `elif user:` branch at :1130 would
otherwise use.

**The fix:** assign to `requestor_user`. One identifier.

### Fix 2 — Reject's user branch dereferences an absent join request

`routes.py:1180-1187`. The community branch (:1157) and the feed branch (:1169)
both guard their bodies with `if join_request:`. The user branch does not:

```python
join_request = session.query(UserFollowRequest).filter_by(...).first()

existing_follow = session.query(UserFollower).filter_by(local_user_id=join_request.user_id, ...)
```

A `Reject` naming a follow request that is already gone — withdrawn, or already
rejected — raises `AttributeError: 'NoneType' object has no attribute 'user_id'`
instead of logging. Peer-triggerable.

**The fix:** give the branch the same `if join_request:` guard its siblings have,
with `requestor_user.num_following -= 1` inside it, matching the sibling
structure.

### Why the fixes come early

Tasks are ordered so no coverage is written against behaviour that is about to
change: harness, then Follow's three branches, then **Fix 1**, then Accept, then
**Fix 2**, then Reject. Writing the Accept suite first and rewriting it after the
fix would waste the work and, worse, would produce tests whose docstrings record
the broken behaviour as though it were the contract.

## What reading has already found

Registered, not fixed. Listed here so the plan has targets and a later reader can
separate what was predicted from what was discovered.

- **`routes.py:1067-1069`** — the Follow/User branch adds its `Notification` and
  commits through `db.session`, while the `UserFollower` row it accompanies went
  through the task-local `session`. Under the queued path these are the same
  object; under the inline path D60 establishes they are not, so the follower and
  its notification land in two different transactions. This is D60's divergence
  made concrete in a single logical write.
- **`routes.py:989-999`** — the Feed reject path logs nothing at all, where the
  Community reject path (:946, :952) logs `APLOG_FAILURE` before rejecting.
- **Three silent success-adjacent paths**: an already-present `CommunityMember`
  (:965), an already-subscribed feed member (:1001), and an existing
  `UserFollower` (:1030) each return without logging anything.
- **`routes.py:1033`** — `is_accepted=auto_accept if auto_accept else None` can
  never be `False`; the column's third state is unreachable from this path.
- **All four Reject log sites** (:1154, :1168, :1178, :1189) pass `APLOG_ACCEPT`,
  so a rejection is recorded as an acceptance. Same class as D63.
- **`routes.py:1151`** — `Reject` handles only `object['type'] == 'Follow'`;
  anything else falls out of the arm silently, logging nothing.
- **`routes.py:1151` and `:1086`** — `core_activity['object']['type']` is read
  unguarded on a peer-supplied object, the shape D49–D52 already register
  elsewhere.
- **`routes.py:1108`** — `User.query.get(...)` inside the Accept community branch
  reaches through Flask-SQLAlchemy's scoped session rather than the task-local
  `session` used on either side of it, a second instance of the mixing pattern.

## Architecture

### Entry

Unchanged from 5a: `dispatch(activity, store_ap_json=True)` from
`tests/test_inbox_dispatch_preamble.py`, which calls `process_inbox_request`
directly — production's DEBUG branch at `routes.py:758-759`. `tests/README.md`'s
harness section, written by 5a Task 10, documents why seeded rows reach the
dispatcher's independent session and where the request-context asymmetry bites.

For `Accept` and `Reject` the preamble resolves the **actor** as community, then
feed, then user (:861-870), so which of the arm's three target branches runs is
decided before the arm is entered. Tests select a branch by choosing what the
outer actor resolves to — the mechanism 5a Task 2 established and mutation-tested.

### Fixtures

Reused: `signing_peer`, `federation_peer`, `http_mock`, `block_outbound_http`,
`redis_double`, and the factories for users, communities, feeds and members.

New, and shared across all three arms: seeded `CommunityJoinRequest`,
`FeedJoinRequest` and `UserFollowRequest` rows. These are the state the Accept
and Reject arms consume, and building them once is most of the argument for
keeping the three arms in one sub-project rather than splitting Follow off.

`send_post_request` is doubled at its binding site on the routes module, the way
5a doubled `process_report` and `process_announce_of_uri`.

### File structure

| file | responsibility |
|---|---|
| `tests/test_inbox_dispatch_follow.py` (create) | the Follow arm's three target branches, the reject and accept sends, the membership writes |
| `tests/test_inbox_dispatch_accept_reject.py` (create) | both Accept forms, both fixes' regression tests, the Reject branches |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | the register, continuing from D64 |
| `coverage_floors.ini`, `tests/README.md` (modify) | the raised floor; any harness fact this slice adds |

Two files rather than four: Follow is self-contained, while Accept and Reject
share the join-request fixtures and are best read against each other — their
branch structures are near-mirrors, and the mirror is where the `APLOG_ACCEPT`
mislabelling and the missing guard both show up.

## The quality bar

Unchanged, and three parts of it bear directly on this code:

**Each half of a compound guard is dropped separately and must die distinctly.**
`local_user.has_blocked_user(...) or local_user.has_blocked_instance(...) or instance_banned(...)`
at :1025 is three alternatives with three different subjects; 5a's Task 7 showed
how easily one seeded row satisfies two of them.

**A mutant killed by `respx.models.AllMockedAssertionError` is an infrastructure
kill, not a behavioural one.** It fires inside a blocked fetch before any
assertion runs. Re-run such a mutant with the fetch served before claiming a
guard is load-bearing. 5a Task 2 found a guard with no semantic effect that way.

**A guard is tested on the whole domain it claims to reject**, not the one
example that motivated it.

No `if TYPE_CHECKING`. No new inline imports.

## Verification

Baseline at `92113827`: `app/activitypub/routes.py` at **510/1813 statements,
203/890 branches, 26.378 % blended, floor 26**. Full suite 2825 passed, 3 skipped.

Covering 172 further statements and their branches should put the module near
35 %. The design commits to raising the floor from the measured figure, one point
below it, not to that projection.

Each task ends with the documented chain — suite, then coverage, then ratchet,
`&&`-chained so a red suite cannot reach the ratchet. The final measurement task
reads `executed_lines` and `missing_branches` against each of the three spans and
**explains every gap**; an unexplained remainder fails that task.

The two fixes each require a test that fails before the fix and passes after,
with the failure recorded in the report — not merely a test that passes
afterwards.

## Risks

**The fixes change behaviour the rest of the suite may depend on.** Fix 1 makes a
previously-always-failing path succeed. Before committing it, the full suite runs,
not just the new file: if anything elsewhere asserted the broken outcome, that is
a finding about the old tests and must be reported, not silently amended.

**Fix 1's blast radius is wider than one identifier.** Making the string-Accept
path reach the target branches means `community`, `feed` and `user` now matter on
a path where they never did. The task that fixes it must establish what the
a.gup.pe actor actually resolves to in the preamble, and cover the resulting
branch, rather than fixing the assignment and declaring victory.

**Outbound sends are the arm's observable output.** Four `send_post_request`
call sites build Accept or Reject documents. Doubling without asserting the
payload would leave the federation contract untested while showing full line
coverage — the exact shape of a green test proving nothing.

**`num_following` arithmetic is unguarded on the Reject path** even after Fix 2:
the decrement runs whenever a join request exists, regardless of whether a
follower row was found. Fix 2 does not address that; it is registered.

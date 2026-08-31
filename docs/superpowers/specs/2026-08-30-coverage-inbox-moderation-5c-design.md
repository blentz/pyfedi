# Sub-project 5c: the inbox dispatcher's moderation arms

`app/activitypub/routes.py`'s `Delete`, `Lock`, `Add`, `Remove` and `Block` arms
inside `process_inbox_request` — 263 statements, 5 of them executed, and those
five are only the `if core_activity['type'] == ...` guard lines that sub-projects
5a and 5b fall through on their way elsewhere.

## Why this, and why now

5a covered the dispatcher's preamble and its dispatch arms; 5b covered the
membership handshake and raised the module from 26.378 % to 35.735 % blended.
Both slices deliberately left the arms that carry *moderator* authority.

These five decide who is banned, who is a moderator, what is deleted and what is
locked. A defect here is not a dropped activity — it is a moderation action that
silently fails, or one that succeeds when the actor had no right to it. Reading
the arms before writing a line of test found ten candidate defects, one of which
makes an entire federated moderation action non-functional.

At 263 statements this is the largest slice of the campaign — 33 % larger than 5a
and 50 % larger than 5b. The project owner chose to keep it whole rather than
split it, and to fix the unguarded dereferences in flight rather than register
them. Both decisions are recorded here because they shape everything below.

**The stated concern, for the record.** A 263-statement slice carrying six
behaviour changes produces the largest and least separable diff of the campaign,
and the coverage work and the fixes become harder to review independently — which
is what the register-don't-fix rule protects. The mitigation is structural: every
fix is its own test-first commit, sequenced immediately before the coverage that
depends on it, so the diff separates by commit even though it does not separate
by sub-project.

## Scope

To full statement and branch coverage or a documented reason, with mutation
evidence per guard:

| lines | statements | arm |
|---|---|---|
| 1264–1327 | 44 | `Delete` — feed deletion, content deletion, the PM path |
| 1356–1395 | 29 | `Lock` — post and comment locking |
| 1396–1468 | 60 | `Add` — feed communities, stickies, moderators |
| 1469–1570 | 76 | `Remove` — Add's mirror, plus the auto-unsubscribe loop |
| 1590–1671 | 54 | `Block` — site bans, community bans, the Mastodon no-target path |

Measured at `63997f59`: 5 executed, 258 missing. Module at 683/1814 statements,
284/892 branches, 35.735 % blended, floor 35.

### Doubled, not entered

`delete_post_or_comment`, `ban_user`, `site_ban_remove_data`,
`community_ban_remove_data`, `add_to_modlog`, `do_subscribe`,
`announce_activity_to_followers`, and `send_post_request`. All are doubled at
their binding site on the routes module, as 5a and 5b established.

One payload is asserted rather than merely counted: the `Undo`/`Follow` document
`Remove` sends at `routes.py:1513` when auto-unsubscribing a feed member from a
remote community. That JSON is a federation contract, and a test that only
counted the send would show full line coverage while leaving it untested.

### Out of scope

`Undo` and its seven sub-types — slice 5d. Create/Update keeps its own spec.
Everything 5a and 5b covered stays covered; this slice adds to the same module
and the same floor.

## The six fixes

This sub-project departs from the campaign's register-don't-fix rule for six
defects, on the project owner's explicit instruction. Each is fixed test-first,
in its own commit, separate from every test-only commit.

The departure is bounded: **no other defect this slice finds is fixed here.**
Everything in "What reading has already found" below is registered, as D1–D79
were.

### Fix 1 — `Lock`'s comment branch cannot complete

`routes.py:1379-1388`. Inside `elif post_reply:`, `post` is guaranteed `None` —
the branch is only reached when the `if post:` above it was falsy. Yet three
statements dereference it:

- `:1380` — `if post_reply.community.is_moderator(mod) or post.community.is_instance_admin(mod):`
  The `or` short-circuits when the actor IS a moderator, so this raises only when
  they are not — turning the permission *refusal* into an `AttributeError`.
- `:1386` — `add_to_modlog('lock_post_reply', actor=mod, target_user=post.author, ...)`
- `:1387` — `community=post.community`

The last two are on the **success** path, so a moderator successfully locking a
comment also raises. Federated comment locking therefore fails on every outcome.

The intent is unambiguous: the branch is about `post_reply`, and `PostReply`
carries both relationships — `author` and `community`, `app/models.py:2899-2900`.

**The fix:** three identifiers, `post.` → `post_reply.`.

### Fix 2 — `Delete`'s feed lookup is dereferenced before its own guard

`routes.py:1270-1279`:

```python
feed = session.query(Feed).filter_by(ap_public_url=core_activity['object']['id']).first()

if not user.id == feed.user_id:          # :1273 — raises when feed is None
    log_incoming_ap(..., 'Delete rejected, request came from non-owner.')
    return

if feed:                                  # :1278
    ...
else:
    log_incoming_ap(..., f"Delete: cannot find {...}")   # unreachable
```

A `Delete` naming a feed this instance does not have raises `AttributeError`
instead of logging, and the `else` branch written to handle exactly that case can
never be reached.

**The fix:** hoist the not-found refusal above the ownership check, so the
missing-feed path logs and returns before anything dereferences `feed`, and the
`if feed:` / `else:` collapses to the straight-line delete it already is.

### Fix 3 — `Delete`'s actor lookup is also unguarded

`routes.py:1268`. `user = find_actor_or_create_cached(actor_id)` can return
`None` — `validate_remote_actor` refuses banned and malformed actors — and
`:1273` then reads `user.id`.

This one was **not** in the set the project owner enumerated; it was found while
verifying Fix 2 and sits in the same statement pair. It is listed separately
rather than folded in silently, and may be moved to the register instead without
disturbing the other five.

**The fix:** refuse a `None` actor with a logged failure before the ownership
check.

### Fix 4 — `Add` dereferences a community it may not have found

`routes.py:1404-1419`. The `FeedItem` creation is guarded:

```python
if community_to_add and isinstance(community_to_add, Community):
    ...create FeedItem, bump feed.num_communities, commit...
# loop is NOT inside that guard:
feed_members = session.query(FeedMember).filter_by(feed_id=feed.id).all()
for fm in feed_members:
    ...
    actor = community_to_add.ap_id if community_to_add.ap_id else community_to_add.name
```

The auto-subscribe loop exists to subscribe feed members *to the community just
added*. When no community was added it has nothing to do, and instead raises on
`community_to_add.ap_id`.

**The fix:** move the loop inside the guard that establishes what it operates on.

### Fix 5 — `Remove` deletes a `FeedItem` it may not have found

`routes.py:1478-1481`. `session.query(FeedItem).filter_by(...).first()` can return
`None`, and `session.delete(None)` raises. The `feed.num_communities -= 1`
immediately after would also decrement for a removal that never happened.

**The fix:** guard both the delete and the decrement on the row existing.

### Fix 6 — `Remove` dereferences a membership it may not have found

`routes.py:1492-1494`. `cm = session.query(CommunityMember).filter_by(...).first()`
can return `None`, and the next line reads `cm.joined_via_feed`.

**The fix:** add `cm and` to the condition, which short-circuits before the
dereference.

### Why the fixes come before their coverage

Tasks are ordered so no coverage is written against behaviour about to change:
harness, then Fix 1 and Lock's coverage, then Fixes 2–3 and Delete's, then Fix 4
and Add's, then Fixes 5–6 and Remove's, then Block, which needs no fix.

Writing an arm's suite first and rewriting it after would waste the work and,
worse, would produce tests whose docstrings record a crash as the contract.

## What reading has already found

Registered, not fixed.

- **`routes.py:1528, 1563, 1566, 1568`** — four `log_incoming_ap` calls in the
  `Remove` arm pass `APLOG_ADD`, while four others in the same arm (`:1524`, `:1540`,
  `:1542`, `:1559`) correctly pass `APLOG_REMOVE`. A removal is recorded as an
  addition. Same class as D63 and D68.
- **`routes.py:1401-1419`** — `Add`'s feed branch logs nothing on any outcome:
  not on success, not when the community cannot be resolved.
- **`routes.py:1664-1668`** — `Block`'s Mastodon no-target path logs nothing,
  and silently does nothing at all when `object` is not a string.
- **`routes.py:1560-1561`** — `add_to_modlog('remove_mod', ...)` runs whether or
  not a membership existed, while `APLOG_SUCCESS` is logged only inside the
  `if existing_membership:` block. A no-op removal writes a modlog entry and logs
  nothing.
- **`routes.py:1281-1293`** — `Delete`'s feed path calls `session.commit()`
  inside each of three delete loops. A failure part-way leaves the feed's items
  or members partly deleted and the feed itself intact — the partially-applied
  shape D13 registered.
- **`routes.py:1612`** — `core_activity['object'].lower()` raises
  `AttributeError` on a dict-shaped object, before the `isinstance` check the
  Mastodon path applies at `:1664`.
- **`routes.py:1424` and `:1530`** — `target = core_activity['target']` is read
  unguarded in both `Add` and `Remove`; a peer omitting it gets a `KeyError`.
- **`routes.py:1500`** — the auto-unsubscribe path special-cases the hardcoded
  domain `ovo.st`.
- **`routes.py:1417`** — `from app.community.routes import do_subscribe` is an
  inline import inside a loop, one of the campaign's catalogued violations.
- **`routes.py:1640-1642`** — `blocked.ban_until` is assigned
  `core_activity['expires']` or `['endTime']` directly, a peer-supplied string
  written to a `DateTime` column with no parsing.

## Architecture

### Entry

Unchanged: `dispatch(activity, store_ap_json=True)` from
`tests/test_inbox_dispatch_preamble.py`, which calls `process_inbox_request`
directly — production's DEBUG branch at `routes.py:758-759`. `tests/README.md`'s
harness section documents why seeded rows reach the dispatcher's independent
session, the request-context asymmetry, and the fakeredis lock limitation.

`Add`, `Remove` and `Block` read `community` and `feed` from the preamble, so a
test selects a branch by choosing what the activity's **actor** resolves to — the
mechanism 5b established and mutation-tested. `Delete` and `Lock` resolve their
own targets from `core_activity['object']`.

### Fixtures

Reused: `make_feed`, `make_community_join_request`, `make_feed_join_request`,
`make_user_follow_request` (5b), plus `make_community`, `make_user`,
`make_post`, `make_post_reply`, `make_community_member`, `make_instance_ban`,
`signing_peer`, `redis_double`, `block_outbound_http`.

New, and needed by three arms: a `FeedItem` factory and a `FeedMember` factory.
5b noted the absence of the latter and hand-built it in three places; this slice
needs both often enough to justify them.

`record_sends` from `tests/test_inbox_dispatch_follow.py` is reused for `Remove`'s
outbound `Undo`.

### File structure

| file | responsibility |
|---|---|
| `tests/factories.py` (modify) | `make_feed_item`, `make_feed_member` |
| `tests/test_inbox_dispatch_lock_delete.py` (create) | `Lock` and `Delete`, and Fixes 1–3's regression tests |
| `tests/test_inbox_dispatch_add_remove.py` (create) | `Add` and `Remove`, and Fixes 4–6's regression tests |
| `tests/test_inbox_dispatch_block.py` (create) | `Block` |
| `app/activitypub/routes.py` (modify, fix tasks ONLY) | the six fixes |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | the register, continuing from D80 |
| `coverage_floors.ini`, `tests/README.md` (modify) | the raised floor and any harness fact this slice adds |

Three test files rather than one or five. `Add` and `Remove` are near-mirrors
sharing the same feed-and-community fixtures and are best read against each
other — the mirror is where the `APLOG_ADD` mislabelling shows up. `Lock` and
`Delete` both resolve their own targets and share the post/reply fixtures.
`Block` shares nothing with either and carries the ban semantics alone.

## The quality bar

Unchanged, and four parts bear directly on this code:

**Each half of a compound guard is dropped separately and must die distinctly.**
`if not community.is_moderator(mod) and not community.is_instance_admin(mod)`
appears in both `Add` (`:1421`) and `Remove` (`:1527`), and
`if not community.is_moderator(blocker) and not community.is_instance_admin(blocker)`
in `Block` (`:1655`). Three instances of the same two-alternative shape.

**A mutant killed by `respx.models.AllMockedAssertionError` is an infrastructure
kill, not a behavioural one.** Re-run with the fetch served before claiming a
guard is load-bearing.

**Beware default-backed assertions.** 5b shipped one — `community.last_active is
not None`, against a column declared `default=utcnow`. Before asserting a
column's value proves a write, check its declared default and seed a
distinguishable starting value.

**A guard is tested on the whole domain it claims to reject.**

No `if TYPE_CHECKING`. No new inline imports — note that `routes.py:1417`
already contains one, registered above and not fixed here.

## Verification

Baseline at `63997f59`: **683/1814 statements, 284/892 branches, 35.735 %
blended, floor 35.** Full suite 2868 passed, 3 skipped.

Covering 258 further statements and their branches should put the module near
50 %. The design commits to raising the floor from the measured figure, one point
below it, not to that projection.

Each fix task runs the **full** suite, not just its own file: six behaviour
changes is where a regression elsewhere surfaces, and anything that asserted the
broken behaviour must be reported rather than amended. Coverage-only tasks run
their own file.

The final measurement task reads `executed_lines` and `missing_branches` against
each of the five spans and **explains every gap**; an unexplained remainder fails
that task.

Each fix requires a test that fails before it and passes after, with the failure
text recorded — not merely a test that passes afterwards.

## Risks

**Six behaviour changes in one sub-project is the campaign's largest such
departure.** Every one makes a previously-raising path complete, so each could
unblock code that nothing has ever executed. The full-suite run after each fix is
the detector; a failure elsewhere is a finding about the existing tests, not a
licence to amend them.

**Fix 1 restores a whole moderation action.** Once `Lock`'s comment branch
completes, it writes `replies_enabled = False` across a subtree via raw SQL
(`routes.py:1382-1385`) and adds a modlog entry. That path has never run in
production against a working branch; its coverage should assert the subtree
update, not merely the flag on the reply itself.

**`Remove`'s auto-unsubscribe loop reaches the network.** It builds and sends an
`Undo`/`Follow` per member for remote communities. Doubling `send_post_request`
is mandatory; leaving it live would make the suite dependent on `block_outbound_http`
raising, which 5a established is an infrastructure signal rather than a
behavioural one.

**`Block` is the one arm with no fix**, and its `blocked.ban_until` assignment
takes a peer-supplied string into a `DateTime` column. Tests should establish
what actually happens rather than assuming SQLAlchemy coerces it.

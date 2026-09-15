# Coverage sub-project 45: two guards that do not guard, and community.py's membership group

**Date:** 2026-09-15
**Branch:** `blentz`
**Measured at:** `6c6f2eb7` (full suite, `PYTEST_EXIT=0`, 4986 passed, 3 skipped, all 25 floors met)

## Goal

Three parts, in this order.

**Part 1 — repair two live guards that do not guard**, and close
`app/shared/site.py` in the process.

**Part 2 — cover `app/shared/community.py`'s membership group**: 88 statements and
53 arcs across six functions.

**Part 3 — a mutation pass** over the covered surface.

Part 1 comes first because both defects are live and Part 2 is not.

## The pattern this round is named for

Three consecutive rounds have now found the same shape by reading the target module
before scoping it:

| Round | Site | The guard | What it does instead |
|---|---|---|---|
| 43 | `app/shared/user.py:161,171,183,192` | `if SRC_WEB:` | tests a constant, so it is always true |
| 44 | `app/shared/auth.py:66-75` | the ban refusal | nested one level too deep, so three of four ban states fall through |
| 45 | `app/shared/site.py:18-19` | "You cannot block the local instance" | flashes, then blocks it anyway |
| 45 | `app/shared/community.py:60` | "step down as moderator first" | De Morgan error, so a plain moderator leaves freely |

**A refusal that does not refuse is this codebase's most repeated defect class**, and it
is invisible to coverage: every one of these lines executes. Only an assertion about
the *outcome* catches them, which is why each was found by reading rather than by a
gap in a report.

## Part 1: the two guards

### 1a. `app/shared/site.py:14-19` — the flash without a return

```
14    if instance_id == 1:
15        msg = 'You cannot block the local instance.'
16        if src == SRC_API:
17            raise Exception(msg)
18        else:
19            flash(_(msg), 'error')
21    existing = db.session.query(InstanceBlock).filter_by(...).first()
22    if not existing:
23        db.session.add(InstanceBlock(user_id=user_id, instance_id=instance_id))
```

The API arm raises at `:17`. **The web arm flashes at `:19` and falls through**, so
`:23` creates the block the message just said was impossible.

**Its twins all return.** `app/shared/user.py:29-31`'s `block_another_user` flashes
then `return`s; `app/shared/domain.py` does the same. `site.py` is the one that does
not.

**It is reachable through ordinary routes**, not only by a crafted call:
`app/post/routes.py:1480` passes `post.instance_id` and `app/user/routes.py:881`
passes `user.instance_id`, **both of which are 1 for local content**. A user clicking
"block instance" on a local post blocks their own instance. Seven web call sites pass
`SRC_WEB`; only `app/api/alpha/utils/site.py:93` uses the protected API arm.

**The fix:** add `return` after `:19`, matching the twins.

### 1b. `app/shared/community.py:60` — the De Morgan error

```
60    if not cm.is_owner or not cm.is_moderator:
61        task_selector('leave_community', ...)
...
70    else:
73            raise Exception('Step down as a moderator before leaving the community')
75            flash(_('You need to step down as moderator before unsubscribing.'), 'warning')
```

`not A or not B` is `not (A and B)`, so the leave path runs unless the member is
**both** owner and moderator.

`CommunityMember.is_moderator` and `.is_owner` are independent booleans, both
defaulting False (`app/models.py:3514-3515`), and federated moderators are created
with `is_moderator=True` and no `is_owner` (`app/activitypub/util.py:959`,
`app/activitypub/routes.py:1473`). For such a member `not cm.is_owner` is True, the
`or` short-circuits, and they leave — **the guard is dead for exactly the population
it names.**

**The fix:** `if not cm.is_owner and not cm.is_moderator:`.

### What Part 1 must prove

The failing observation comes first, as in sub-projects 40 through 44. For **1a**,
pin that `block_remote_instance(1, SRC_WEB)` **both flashes the error and creates the
`InstanceBlock`** — the second half is the defect and the first half is what hides
it. For **1b**, pin that a member with `is_moderator=True, is_owner=False` leaves
successfully, and that the `CommunityMember` row is gone afterwards.

Then the fixes, then the pins inverted. **Keep a control in each**: for 1a, the API
arm, which already raises and must keep raising; for 1b, a member who is neither
owner nor moderator, who must still be able to leave.

## Part 2: `app/shared/community.py`'s membership group

### Decomposition, measured from the full-suite JSON at `6c6f2eb7`

`app/shared/community.py` is 688 lines and 17 functions at 11.584% — 410 statements
and 193 arcs missing, which is three to four rounds at this campaign's pace. It
splits into four groups that sum exactly:

| Group | Functions | stmts | arcs |
|---|---|---|---|
| **A — membership and subscription (THIS ROUND)** | `join_community`, `leave_community`, `block_community`, `unblock_community`, `subscribe_community`, `favorite_community` | **88** | **53** |
| B — moderation | `add_mod_to_community`, `remove_mod_from_community`, `delete_community`, `restore_community` | 99 | 40 |
| C — creation and editing | `make_community`, `edit_community` | 139 | 54 |
| D — invites and flair | `invite_with_chat`, `invite_with_email`, `create_invite_token`, `get_comm_flair_list`, `comm_flair_ap_format` | 84 | 46 |

Per function, missing statements then missing arcs: `favorite_community` 30/18,
`leave_community` 16/8, `join_community` 15/10, `block_community` 11/6,
`unblock_community` 11/6, `subscribe_community` 5/5.

**Group A is first because the harness already exists.** `block_community` and
`unblock_community` are the fifth instance of a block/unblock twin pair this campaign
has covered — after `block_another_user` (sub-project 43), `block_domain`
(sub-project 44) and `block_remote_instance` (Part 1 of this round). The seeding
conventions in `tests/test_shared_domain.py` transfer directly: the `src` fork,
`bearer`, `web_ctx`, and the id-1 burn.

**With `site.py`'s 2 statements and 4 arcs this round covers 90 statements and 57
arcs** — light against the 110-130 statement pace, comfortable on arcs. That is
deliberate: extending Group A with `delete_community` and `restore_community` would
reach 118/73 but would split the moderation group, and the twin-family cohesion is
what makes the harness transfer.

### What is new here, and what the sibling files do not prepare

- **`join_community:36-38` chooses sync or async** — `send_async = not (current_app.debug or src == SRC_WEB)`, then `task_selector('join_community', send_async, ...)`, and `:40` branches on `send_async or sync_retval is True`. None of the closed siblings has this shape. Both arms need exercising, and `SRC_PLD` at `:50` is a third return value no twin has.
- **`leave_community:59` uses `.one()`**, which raises `NoResultFound` for a non-member. Its siblings use `.first()`. Register the divergence.
- **`subscribe_community` is nearly closed already** — 5 statements and 5 arcs — so its oracle is not the file named after it. **Measure before assuming**, as this campaign now requires.
- **`favorite_community` is the group's largest at 30/18** and has no twin at all.

## Part 3: verification

A mutation pass **scoped by the statement list, not the arc table**, with the
statement and compound lists derived mechanically via `ast.walk` and **the derivation
command and its raw output published beside every count**.

**Measure the oracle before using it.** Sub-project 43 twice prescribed test files
that did not execute the function under test, which turns every mutant there into a
survivor; sub-project 44 avoided it by checking first, and found that
`app/shared/auth.py`'s web arm is covered by `tests/test_redirect_targets.py` and
`app/shared/upload.py`'s SVG paths by `tests/test_utils_security.py`. **Confirm each
oracle reproduces the measured figure before mutating anything.**

**Two mandatory mutations beyond the derived list**, one per production fix:

1. **Delete the `return` added at `site.py:19`.** The 1a pins must fail. If nothing
   dies, the fix is decorative.
2. **Restore `community.py:60`'s `or`.** The 1b pins must fail.

Standing rules: a crash kill is not a kill unless a viable non-crashing variant also
dies; fix-catching is not a unique kill; **an equivalence claim needs a proof of
unkillability, never a failure to kill**; **name the cause from `tests/README.md`
fact 75 that actually fits, and if none does, say so plainly rather than force-fitting
one** — two citations were corrected in sub-project 44, one for bending a quotation
and one for naming a cause that does not exist.

## Production changes

**Two, and both are named here before the round begins**: the `return` at
`site.py:19` and the `and` at `community.py:60`. Each is one line. Anything else that
surfaces is **registered, not fixed** — sub-projects 40 through 44 each named their
production changes in advance and each held to the count.

## Success criteria

- Both guards fixed, each pinned first and each pin inverted, with both controls still
  passing unchanged.
- `app/shared/site.py` at `missing_lines []` and `missing_branches []`, **checked as
  lists**, and a **new** `coverage_floors.ini` entry at its measured value.
- Group A's six functions at `[]`/`[]`, with any line ruled unreachable carrying a
  **named** fact 75 cause and a proof — or an explicit statement that no catalogued
  cause fits.
- `app/shared/community.py`'s floor is **not** set this round: the module is one group
  of four and a floor would ratchet against work not yet done. Record the measured
  percentage in the register instead.
- Full suite green, floors check chained with `&&` and **both** arguments against a
  `--cov=app` JSON — a narrow report makes the checker count absent modules as 0.0.
- No regression in the five closed modules: `post.py`, `reply.py`, `user.py`,
  `domain.py` and `site.py` must all measure 100 with `[]`/`[]`.
- Findings registered from **D595**; `tests/README.md` facts from **256**.

## Out of scope, carried forward

- **Groups B, C and D of `community.py`** — 322 statements and 140 arcs, two to three
  further rounds.
- **`app/shared/feed.py`** — 334 statements and 153 arcs, the last large `app/shared`
  sibling, still undecomposed.
- **The register's backlog, which is now the campaign's largest unowned liability.**
  D538 called it that two rounds ago and every round since has lengthened it: roughly
  100 carried mutation survivors with recipes, **D594's live CDN defect** (purging a
  user never flushes their uploaded images), **D543's open code half** (five unguarded
  arithmetic sites), and the two taxonomy proposals D578 and D589 that no module round
  can settle. **A round dedicated to it was the recommended alternative to this one
  and remains available.**

# Sub-project 27: closing `app/shared/tasks/deletes.py`

**Date:** 2026-09-08
**Branch:** `blentz`
**Predecessor:** sub-project 26, commits `7df08aea..296a8c79`, kept as-is

## Goal

Take `app/shared/tasks/deletes.py` from 11.16% to 100% statement and branch
coverage, land three production changes, close D309, and correct a register
entry that is currently wrong in a way that would misdirect this very round.

Measured at `296a8c79`: 201 statements, 173 missing, 50 branches, 0 partial.
Comparable to sub-project 25's locks + likes at 202 statements.

## Why this module, and why now

`deletes.py` holds the last omitting site of **D309**, a finding tracked since
sub-project 20 across twelve federation gates. Ten have been closed by seven
consecutive sub-projects. This one closes the finding.

It is also the module where the campaign's accumulated harness knowledge pays
off most: `delete_object` fans out over `following_instances()` (fact 141),
signs with three different actors depending on path (fact 147's cousin), and
builds four distinct `@context` nesting shapes. Every one of those is a
mechanism a previous sub-project learned the hard way.

## THE REGISTER IS WRONG ABOUT THIS MODULE, AND THAT IS THE FIRST DELIVERABLE

D309's cell states:

> Checked directly at this commit: `deletes.py:127` is
> `if not is_post and community.local_only:` and `:130` is
> `if not followers and community.local_only:` -- neither carries `private` or
> `online()` in any form. So `delete_object` is now the LAST survivor of the
> WIDE shape ... Fixing it ... means adding two disjuncts at two lines, not one
> disjunct at one.

That is false. The gate spans `:127-134`:

```python
127:     if not is_post and community.local_only:
128:         return
129:     followers = session.query(UserFollower).filter_by(local_user_id=user.id).all()
130:     if not followers and community.local_only:
131:         return
132:
133:     if not community.instance.online():
134:         return
```

`online()` **is** checked, at `:133`, as its own statement rather than as a
disjunct. Only `private` is missing. `delete_object` is the **narrow** shape.

**How the error was made and how it survived review.** The claim entered
through sub-project 26's Task 11 dispatch, which asserted the wide shape on the
strength of a `sed -n '125,132p'` window — a window that stopped one line short
of its own disproof. The Task 11 reviewer then verified the claim by opening
`:127` and `:130`, both of which are quoted correctly. The sentence is true
about the two lines it cites and false about the guard they belong to.

This is the second Critical register error in two consecutive sub-projects
(D329 was the first), and both have the same shape: **a claim about a BLOCK
verified against the LINES it cites.** The register must record the mechanism,
not only the correction — a citation-level check cannot falsify a block-level
claim, and every "checked directly at this commit" in the register that
describes a multi-line construct is exposed to it.

## Production changes

Three, matching sub-projects 24, 25 and 26.

### PC1 — D309's last site, `:133`

```python
    if community.private or not community.instance.online():
```

One disjunct, in place, file length unchanged. This closes D309 at its twelfth
and final site.

The other family members put `private` after `local_only` in a single
expression. This function cannot: its `local_only` checks are conditional on
`is_post` and on `followers`, so `private` goes on `:133`, the unconditional
guard. The register must record that the site's shape differs from the family's
while its effect does not.

### PC2 — the crash at `:252`

`delete_posts_with_blocked_images` calls:

```python
252:                     delete_object(user_id, post, is_post=True, reason='Contains blocked image')
```

`delete_object`'s signature is `(..., session=None)` at `:118`, and `:119` is
`user = session.query(User).get(user_id)`. `deletes.py` never references
`db.session`, so `patch_db_session` does not rescue it. Every call raises
`AttributeError: 'NoneType' object has no attribute 'query'`.

The fix is `session=session`, matching all six other call sites in the file.

**The failure is destructive, not clean.** The loop commits at `:250` and
`file.delete_from_disk()` runs at `:249` before the crash at `:252`. So a batch
blocked-image removal deletes exactly one post, unlinks its file, raises,
skips the rest of `post_ids`, and federates no `Delete` for any of them. Two
live callers: `app/admin/routes.py:2320` and `app/post/routes.py:2153`.

This is D319's shape from sub-project 24 — a feature that has never worked,
found by reading a module coverage was about to touch.

### PC3 — the notification cleanup, `:205-206`

```python
205:     if reason:
206:         return
```

`reason` is set only on moderator paths (`app/shared/post.py:1065`, `:1107`;
`app/shared/reply.py:428`, `:464`); the author's own delete passes none
(`post.py:779`, `:813`; `reply.py:261`, `:289`). So the early return means "a
moderator removal is not broadcast to the author's personal followers", which
is defensible and looks deliberate.

The notification cleanup at `:221-228` is merely downstream of the same
`return`, and that is not deliberate. A moderator-removed post keeps its
notifications; an author-deleted one loses them. The asymmetry runs the wrong
way: moderated removals are exactly where stale notifications pointing at
removed content matter.

Fix: delete `:205-206`, and change `:208` to

```python
    if is_post and followers and not reason:
```

This preserves the fan-out suppression exactly and frees the cleanup.

**PC3 removes two lines and shifts every citation below `:206` by −2.** That is
the rot that has bitten this campaign four times, twice inside the commit that
caused it. **PC3 therefore lands EARLY, in its own task, before any test
docstring cites into the region below it.** Sequencing this correctly costs
nothing; sequencing it wrongly costs a citation sweep across the whole file.

## Module structure

320 lines. Four regions:

**Six thin wrappers** (`:28-116`) — `delete_reply`, `restore_reply`,
`delete_post`, `restore_post`, `delete_community`, `restore_community`. All
identical: `current_app.app_context()`, `get_task_session()`,
`patch_db_session`, one query, one `delete_object` call, then
`except Exception: session.rollback(); raise` and `finally: session.close()`.

**`delete_object`** (`:118-228`) — the engine. Guards, envelope, three delivery
paths, notification cleanup.

**`delete_posts_with_blocked_images`** (`:232-257`) — batch deletion, currently
crashing (PC2).

**PM handling** (`:261-320`) — `delete_pm` and `restore_pm` over
`delete_message`, which returns early for local recipients at `:294-295` and
otherwise sends one request to `recipient.ap_inbox_url`.

## Four delivery paths and three signers

| # | Site | Condition | Signer |
|---|------|-----------|--------|
| 1 | `:196-199` | `community.is_local()` | community key |
| 2 | `:201-203` | remote community | user key |
| 3 | `:214-218` | `is_post and followers` (and, after PC3, `not reason`) | user key |
| 4 | `:320` | PM, remote recipient | sender key |

Paths 1 and 3 can both run in one call. Path 3 skips any domain already in
`domains_sent_to` (`:217`), which paths 1 and 2 populate.

**`:197` is the widest recipient guard in the package** — four conjuncts:

```python
instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain)
```

coverage.py records one arc pair for the whole `if`, so branch coverage cannot
see three of the four. Each needs its own discriminating test and its own
mutation. Fact 142 is the governing note.

**Signer identity needs `_key_id_of`.** Sub-project 26's final review found six
signer sites asserted in prose and pinned by none. This module has three
distinct signers across four paths; every one gets a `keyId` assertion.

## Four `@context` nesting shapes

`:181`'s `del delete['@context']` sits inside the `is_local()` branch, and
`:178`'s `del undo['@context']` likewise. So the remote path ships whatever
`:152` and `:168` set.

| Case | Top level | Nested |
|------|-----------|--------|
| Remote, delete | `delete`, has `@context` (`:152`) | — |
| Remote, restore | `undo`, has `@context` (`:168`) | `delete`, absent (deleted `:161`) |
| Local, delete | `announce`, has `@context` (`:192`) | `delete`, absent (deleted `:181`) |
| Local, restore | `announce`, has `@context` (`:192`) | `undo` absent (`:178`), `delete` absent (`:161`) — two depths |

Fact 136 governs: `app/activitypub/signature.py:100-101` reinjects `@context`
at the **top level only**, so every top-level assertion here is vacuous and
only the nested absences discriminate. The local-restore case is the first in
this campaign with absences at **two** nesting depths, which fail
independently and need separate assertions.

## Test architecture

One file: `tests/test_shared_tasks_deletes.py`.

**Oracles.** A spurious send is invisible to a delivered-inboxes set and to a
route call count, because `signature.py:143`'s `except Exception as e:`
swallows respx's unmatched-request assertion into an `ActivityPubLog` failure
row (fact 148, D332). Every "nothing else was sent" claim is a row count.
`post_request` writes its row unconditionally at `signature.py:105`, before the
transport, so a send to a `None` inbox leaves a row and no httpx request.

**Fixtures.** Path 1 needs a `CommunityMember` on the recipient instance
(fact 141); path 3 needs a `UserFollower` row and no membership; path 4 needs
neither. Three helper shapes, and one built for the wrong path yields zero
recipients under assertions that still pass — the failure sub-project 26 hit.

**Duplicated helpers.** `_recording_task_session` becomes the tenth copy and
`_key_id_of` the seventh. Both are deliberate and both increment D324, whose
count is re-derived from the tree rather than incremented by arithmetic.

**Wrapper rollback.** All six wrappers plus the two PM wrappers assert
`record.calls == ['rollback', 'close']`. Sub-project 26 shipped four such
tests asserting only `pytest.raises`, and a mutation replacing `rollback()`
with `pass` survived. Not repeating that.

## Findings to register

Next free is **D333**. Facts continue from **149**.

- **The D309 correction** — in place, with the block-versus-line mechanism
  stated. Not a new number; D309's own cell.
- **`delete_posts_with_blocked_images`'s missing `session`** (PC2) — fixed this
  round, with the destructive partial-batch behaviour recorded.
- **The notification asymmetry** (PC3) — fixed this round.
- **`:214-218`'s unguarded follower fan-out** — the `Instance` join at `:215`
  filters `gone_forever == False` only, not `dormant`, and `:218` sends to
  `instance.inbox` with no `None` check. Registered, not fixed: it is D321's
  shape in a third file, and unlike `blocks.py:159` it carries no inline guard
  at all. Fixing it is a federation-behaviour change this round has not
  budgeted.
- **The `cc` list aliased between `delete` and `undo`** — `:146` binds one
  list; `:155` and `:171` both reference it; `:213` appends to it. `:186`
  rebinds the *name* for the announce, so `announce['cc']` is a separate list.
  No behavioural difference today, since exactly one of `delete`/`undo` is ever
  sent. Registered as a latent aliasing hazard, not fixed.
- **`:213` mutates after `:198` has already sent** — on the local path the
  announce is serialised at `:198`, before `:213` appends follower URLs to the
  nested object's `cc`. The appended URLs never reach the wire on that path.
  Correct as it stands; recorded so a future reader does not "fix" the ordering.

## Deliverables

- `tests/test_shared_tasks_deletes.py`, new.
- `app/shared/tasks/deletes.py` at 100% statement and branch coverage,
  318 lines after PC3.
- Three production changes: PC1 `:133`, PC2 `:252`, PC3 `:205-208`.
- `coverage_floors.ini` gains `app/shared/tasks/deletes.py = 100` — 18 entries.
- Mutations covering all four conjuncts of `:197`, both `local_only` guards,
  PC1's new disjunct, PC2's `session=`, and PC3's `not reason`.
- D309 closed and corrected; new findings from D333; facts from 149.
- Full suite green, all 18 floors met against a report whose mtime postdates
  the run.

## Risks

**The suite's 600s `session_timeout`.** Sub-project 26 established that the
limit is hit when the container stack has accumulated state across consecutive
runs, and NOT because of coverage instrumentation — the same suite ran 235s and
302s with coverage and 434s without. Run `./run_tests.sh --down` before any
full-suite run that matters. This is not a new risk and not this module's
doing.

**PC3's line shift.** Addressed by sequencing PC3 early. Any task that cites
below `:206` must re-derive after PC3 lands.

**`:197`'s four conjuncts.** Branch coverage will read 100% with three of them
untested. Only mutation demonstrates otherwise, so `:197` gets four mutations
and no argument.

**Path 3 is currently unreachable by any test.** It requires `is_post`,
`followers`, and — after PC3 — no `reason`. Building that fixture is the
sub-project's most likely source of a silently-zero-recipient test.

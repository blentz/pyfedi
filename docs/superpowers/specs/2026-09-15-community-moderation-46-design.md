# Sub-project 46: community.py's moderation and lifecycle group

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `3c06e22b`
**Predecessor:** sub-project 45, which closed `app/shared/site.py` at 100.0 and
took `app/shared/community.py` from 11.584 to 32.258 by covering its membership
group.

## Goal

Cover Group C of `app/shared/community.py` — the moderation and lifecycle
functions — and repair two production defects found while scoping, each pinned
by a test that asserts the bug before the fix lands.

This is the second of three rounds that together finish `community.py`. The
module still takes **no coverage floor**: a floor set now would ratchet against
Group B and Group D, which are not yet written.

## Targets

Measured from the delivered tree's own `--cov=app` JSON at `11d6f3a3`, not
carried forward from an earlier spec:

| function | line | missing statements | missing arcs |
|---|---|---|---|
| `add_mod_to_community` | `:540` | 44 | 14 |
| `remove_mod_from_community` | `:608` | 27 | 10 |
| `delete_community` | `:487` | 14 | 8 |
| `restore_community` | `:516` | 14 | 8 |
| **total** | | **99** | **40** |

99 statements and 40 arcs sits inside the campaign's established pace of
110-130 statements and 56-72 arcs. The remaining two groups, for the record and
so a later round need not re-derive them:

- **Group B, lifecycle:** `make_community:213` (54/18) and
  `edit_community:293` (85/36) — 139/54.
- **Group D, invites and flair:** `invite_with_chat:121` (38/22),
  `invite_with_email:189` (13/6), `create_invite_token:176` (7/2),
  `get_comm_flair_list:648` (8/6), `comm_flair_ap_format:665` (18/10) — 84/46.

99 + 139 + 84 = 322 and 40 + 54 + 46 = 140, which reconciles against the
module's measured 322/140.

## The two production changes

Both are in `remove_mod_from_community`. **The budget is two**, the same budget
that held in sub-projects 44 and 45.

### Change 1 — `:622`'s missing `else`: a success that did not succeed

```python
620    existing_member = db.session.query(CommunityMember).filter(CommunityMember.user_id == old_moderator.id,
621                                                   CommunityMember.community_id == community_id).first()
622    if existing_member:
623        existing_member.is_moderator = False
624        existing_member.is_owner = False
625        db.session.commit()
626    if src == SRC_WEB:
627        flash(_('Moderator removed'))
```

`if existing_member:` has no `else`. Remove a moderator who is not a member and
nothing is written — but `:627` still flashes *"Moderator removed"*, `:629`
still calls `add_to_modlog('remove_mod', ...)` naming the target, and `:642`
still fires the `remove_mod` task. **The moderation log, an audit record,
records a removal that never happened.**

This is the campaign's signature defect class inverted. Sub-projects 43, 44 and
45 each found a refusal that does not refuse; this is a success that did not
succeed. It shares the property that makes that class dangerous: **every line
executes, so coverage cannot see it.** Only an assertion about the outcome —
here, that no `ModLog` row was written — catches it.

**The fix:** an `else` that refuses through the module's established source
fork, before the modlog and task run.

```python
    else:
        msg = 'That user is not a moderator of this community.'
        if src == SRC_API:
            raise Exception(msg)
        else:
            flash(_(msg), 'warning')
            return
```

This is the same shape as `:72-76`, the guard sub-project 45 repaired, and as
`app/shared/site.py:16-20`. Asking to remove a moderator who is not one is a
caller error, and the audit log should record nothing because nothing happened.

The idempotent alternative was considered and rejected. It has real precedent —
`block_remote_instance` and `block_community` are both idempotent, and
sub-project 45 covered `test_block_remote_instance_is_idempotent` explicitly —
but idempotency is right when the end state is what was requested. Here the
caller named a specific person as a moderator and that premise is false.

### Change 2 — `:624` can strip a community's last owner

`:624` sets `existing_member.is_owner = False` with no guard. The route that
does the same thing refuses:

```python
# app/community/routes.py:1477-1478
if community.num_owners() == 1:
    flash(_('A community must have one or more owners. Make someone else an '
            'owner before removing this owner.'), 'error')
```

Same operation, two paths, opposite answers. The shared-layer function silently
permits what the route forbids, so **the codebase contradicts itself about
whether an ownerless community is legal.**

This is the mirror of **D609**, registered by sub-project 45: D609 records that
a sole non-moderator owner has *no* exit, because both the route and
`leave_community` refuse them. This finding records that they *do* have one —
through `remove_mod_from_community` — and that taking it leaves the community
with no owner at all. Fixing this makes the two paths agree on the invariant the
route already asserts; it does not resolve D609's dead end, which remains open
and is a design question rather than a coverage fix.

**The fix:** guard on `community.num_owners() == 1` using the same source fork
and the same wording the route already uses. `num_owners` is `app/models.py:762`
and counts `moderators()` whose `is_owner` is set.

The alternative of not clearing `is_owner` at all — leaving ownership entirely
to `community_remove_owner` — was considered. It makes the contradiction
structurally impossible rather than merely guarded, and the function's name
argues for it. It was rejected because it silently changes behaviour for the
legitimate owner-and-moderator demotion, and any caller relying on the combined
effect would break without a test noticing.

### Both changes are pinned first

Each gets a test that asserts the **buggy** behaviour and passes against the
current code, exactly as sub-projects 44 and 45 did. The pin is then inverted
in the same round, and the fix round must show **exactly the predicted set of
tests failing** — no more, no fewer. A control that dies alongside the pin was
not the control it claimed to be, and that is a finding rather than a nuisance.

## Registered, not fixed

Per the standing policy that the register backlog waits until coverage is
complete, and because the round's production budget is spent on the two above:

1. **`delete_community:493` uses `.get()` where `restore_community:522` uses
   `.filter_by(id=community_id).one()`.** Divergent twins. A missing community
   id gives `restore_community` a clean `NoResultFound` and gives
   `delete_community` an `AttributeError` on `None.is_owner` at `:494`. Same
   divergence class as **D598**, which recorded `leave_community:59`'s `.one()`
   against its siblings' `.first()`.

2. **`delete_community:494` permits any moderator to delete a whole
   community** — `community.is_owner(user) or community.is_moderator(user) or
   user.is_admin_or_staff()`. Whether that is intended is a live question in the
   code itself: the author's comment at `:505` already contemplates a
   `deleted_by` column "so a mod can't restore a community deleted by an admin".
   Registering it rather than deciding it.

## Shapes the tests must handle

- **The source fork.** All four functions branch on `src == SRC_API` for
  `authorise_api_user(auth, return_type='model')` versus `current_user`, and
  again at the end for the return value. `SRC_API` returns `user.id`; the web
  arm returns `None`.
- **Divergent error strings for the same condition.** `add_mod:550` raises
  `'no_permission'` where `remove_mod:618` raises `'incorrect_login'`, and
  `delete_community:495` and `restore_community:524` raise `'incorrect_login'`
  for a *broader* condition. Assert the actual strings; do not assume symmetry.
- **`add_mod:549` and `remove_mod:617` are `not A and not B`** — De Morgan
  correct, unlike the `:60` guard sub-project 45 repaired. Both operands need
  independent exercise or one stays deletable. Sub-project 45's Task 4 review
  found exactly this shape unexercised at `:68`; two tests that both fix one
  operand isolate only the other.
- **`add_mod:564`'s local/remote fork.** A local new moderator gets a
  `Notification` under `force_locale(get_recipient_language(...))`; a remote one
  gets a `Conversation` and `send_message`. Both arms need cover, and the remote
  arm has its own `if not existing_conversation:` branch at `:579`.
- **`add_mod:548` filters `banned=False`; `remove_mod:616` does not.** A banned
  user cannot be added as a moderator but can be removed as one. That asymmetry
  looks deliberate — you want to demote a banned user — so it is a test case,
  not a finding.
- **Eight `cache.delete_memoized` calls in each mod function.** Per **D602**,
  this class is unkillable under `tests/conftest.py:68`'s
  `CACHE_TYPE = 'NullCache'`, and **D589** proposes a ninth fact-75 cause for
  it. Do not mutate them and do not claim them as covered behaviour.
- **`delete_community` and `restore_community` are twins** and should be tested
  as twins, with the `.get()`/`.one()` divergence above asserted rather than
  smoothed over.

## Success criteria

- Both defects fixed, each pinned first and each pin inverted, with every
  named control still passing unchanged.
- Group C's four functions each at `missing_lines []` and
  `missing_branches []`, checked as lists. Any line ruled unreachable carries a
  **named** fact 75 cause and a proof, or an explicit statement that none fits —
  fact 75 has **no cause 4(c)**, and cause 8 is narrowly scoped to a
  `try`/`except` whose body can never run.
- **No floor for `app/shared/community.py`**, which stays at 26 floors total.
  Record its measured percentage in the register instead.
- No regression in the five closed modules: `post.py`, `reply.py`, `user.py`,
  `domain.py`, `site.py`.
- Full suite green; floors check chained with `&&` and **both** arguments
  against a `--cov=app` JSON.
- A mutation pass scoped to Group C's four functions, with both fixes as
  mandatory mutations. Report the kill count **scoped to what was mutated**, as
  D602 does — never as an unqualified "zero survivors".
- Findings registered from **D611**; `tests/README.md` facts from **261**.
- **Exactly two production changes.**

## Environment

Unchanged from sub-project 45, and all of it still binding: no host Python with
flask or pytest, so everything goes through `./run_tests.sh`; coverage takes the
dotted module form and a path form silently collects nothing; coverage JSON is
written outside the repo and lands in the container's `/tmp`;
`tests/check_coverage_floors.py` takes two arguments and counts a floored module
absent from the report as 0.0; only the controller runs the full suite, one
pytest session at a time, and a killed pytest corrupts the test database —
recover with `./run_tests.sh --down`.

Sub-project 45 measured the harness for the first time and registered it as
**D610**: peak 745 MB without coverage and 734 MB with, plateau ~685 MB,
coverage costing about 18 MB, 286.98s without coverage and 368.61s with. Three
host OOM kills occurred during that round. If a run dies, read D610 before
attributing a cause — that entry exists because three causes were asserted
before anything was instrumented, and the instrumentation contradicted all
three.

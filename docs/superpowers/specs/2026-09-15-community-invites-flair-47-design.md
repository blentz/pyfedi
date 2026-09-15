# Sub-project 47: community.py's invite and flair group

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `04e4fb85`
**Predecessor:** sub-project 46, which closed `app/shared/community.py`'s
moderation group and took the module from 32.258 to 53.791.

## Goal

Cover Group D of `app/shared/community.py` — the invite trio and the flair pair
— and repair one production defect that appears at two sites, pinned at both
before it is fixed.

This is the third of four rounds finishing `community.py`. The module still
takes **no coverage floor**: Group B remains, and a floor set now would ratchet
against work not yet written.

## Targets

Measured from the delivered tree's own `--cov=app` JSON at `04e4fb85`:

| function | line | missing statements | missing arcs |
|---|---|---|---|
| `invite_with_chat` | `:121` | 38 | 22 |
| `comm_flair_ap_format` | `:684` | 18 | 10 |
| `invite_with_email` | `:189` | 13 | 6 |
| `get_comm_flair_list` | `:667` | 8 | 6 |
| `create_invite_token` | `:176` | 7 | 2 |
| **total** | | **84** | **46** |

84 statements and 46 arcs sits inside the campaign's pace of 110-130 statements
and 56-72 arcs, with the highest arc-to-statement ratio of the module's
remaining groups — more branch-shaped work per line than the raw count suggests.

After this round only **Group B** remains: `make_community:213` (54/18) and
`edit_community:293` (85/36), 139/54. 84 + 139 = 223 and 46 + 54 = 100, which
reconciles against the module's measured 223/100.

## The measurement trap this round must not walk into

**No test file names any of the five functions.** Verified with
`/usr/bin/grep -rln` on each. But `get_comm_flair_list` measures **4 of 12
statements covered**, because production callers reach it from tests of other
modules:

- `app/api/alpha/views.py:602`
- `app/community/routes.py:677` and `:2474`
- `app/post/routes.py:315` and `:737`

All five pass a `Community` object, which is why exactly the
`isinstance(community, Community)` branch and the final query are green.

**Consequence:** a coverage run scoped to the new test file alone will report
`get_comm_flair_list` at **12** missing rather than 8, because that indirect
coverage is absent. The 84/46 target is a **full-suite** figure. Per-task
measurement against the new file is still the right working signal, but the
round's success criteria are settled by the `--cov=app` full-suite run, and any
per-function claim must say which measurement it came from.

This is the fourth round running in which a naive oracle reading would have
produced a wrong number. The previous three were a match inside a docstring, a
name collision across modules, and a function with no oracle at all.

## The production change

**One defect, two sites.** The budget is two changes, the same budget that held
in sub-projects 44 and 45 and that 46 exceeded only to complete a fix it had
already claimed.

```python
# invite_with_chat
130        community: Community = db.session.query(Community).get(community_id)
131        if community.banned:

# invite_with_email
196    community: Community = db.session.query(Community).get(community_id)
197    if community.banned:
```

`.get()` returns `None` for an absent id, so the next line raises
`AttributeError: 'NoneType' object has no attribute 'banned'`. The sibling form
in this same module is `db.session.query(Community).filter_by(id=community_id).one()`
(`restore_community:522`), which raises `NoResultFound` — an error a caller can
recognise and handle.

**This is a recurrence, not a new class.** **D613**, registered by sub-project
46, records the identical shape at `delete_community:493`. Fixing these two
closes the class at two of its three known sites; `delete_community:493` is left
for a later round, because it sits in a group already closed and reopening a
closed function's coverage for an unrelated fix is how a round's budget escapes.

**The fix:** `.get(community_id)` becomes `.filter_by(id=community_id).one()` at
both sites. Each is pinned first — a test asserting the `AttributeError` that
passes against today's code — then fixed, then the pin inverted to assert
`NoResultFound`. The fix round must show **exactly the predicted set of tests
failing**, no more and no fewer.

## Registered, not fixed

1. **`get_comm_flair_list:668-679` has no `else`.** Three `isinstance` branches
   cover `int`, `Community` and `str`; anything else leaves `community_id`
   unbound and `:681` raises `UnboundLocalError`. The signature declares
   `Community | int | str`, so an out-of-contract argument is a caller error —
   but it fails with an error naming an internal variable rather than the
   contract it broke.

2. **`comm_flair_ap_format` returns `None` from a `-> dict` signature** at
   `:691` and `:699`. Both are bare `return` statements on paths the function
   reaches deliberately: a flair that cannot be found, and one whose
   `get_ap_id()` yields nothing. Callers annotated to expect a `dict` get
   `None`.

3. **Neither invite function checks permission, and both carry a dead API
   path.** The gate is `community.can_invite()` at
   `app/community/routes.py:2601`, in the route rather than the shared function.
   Both functions accept `src` and `auth`, but `/usr/bin/grep -rn
   "invite_with_chat\|invite_with_email" app/` finds no API caller, so the
   `SRC_API` branches at `:122-124` and `:190-192` are unreachable in
   production. This is the latent form of the contradiction **D612** repaired:
   a route that guards and a shared function that does not, where the first API
   caller added would bypass the gate. Registering the shape now means the next
   person to add that caller finds it already described.

## A shape to test, explicitly NOT a defect

`invite_with_email:200-202` sets `subscribe = f'accept_invite/{user.lemmy_link()}'`
where `invite_with_chat:148` uses `create_invite_token(community, recipient, user)`.
The emailed link therefore carries the **inviter's handle** rather than an
invitation token.

That looks wrong and is not. `community_invite_accept`
(`app/community/routes.py:2641`) opens with `if '@' in token:` at `:2644` and
flashes *"Ask %(token)s to send an invite to %(current_user)s"*. A
`lemmy_link()` contains an `@`, so the route detects this case deliberately. The
reason is structural: `create_invite_token` writes a `CommunityInvitation` row
keyed on `recipient.id`, and an email invitee has no account yet, so no token
can exist for them.

**Record it as a tested shape**, so that a later round reading `:202` beside
`:148` does not "fix" a deliberate fallback. The tests should assert the
`lemmy_link()` form is what reaches the template.

## Shapes the tests must handle

- **`invite_with_chat:129` is a three-operand guard** —
  `if recipient and not recipient.banned and not instance_banned(recipient.instance.domain):`.
  Each operand needs a test where it alone fails, or the others stay deletable.
  Note sub-project 46 found `delete_community:494`'s three-operand guard had a
  **subsumed** first operand, provably unkillable (fact 75 cause 3); check
  whether these three are genuinely independent before assuming they are.
- **Four paths return `0` indistinguishably** — `:129`'s false arm via `:173`,
  `:132`'s banned community, and `:172`'s `1 if reply else 0`. The caller at
  `app/community/routes.py:2614` sums these into a count it reports to the user.
  Assert *which* path was taken, not merely the return value.
- **`invite_with_chat:144-168` is a four-way software fork** — local recipient,
  `piefed`/`pylova`, `lemmy`/`mbin`, and an else that re-renders the whole
  message from a template. Each has an `INVITE_APPLY` sub-branch, and the
  `piefed` arm has a further `community.local_only` split. This is where the
  round's 22 missing arcs mostly live.
- **`create_invite_token:179` forks on an existing invitation.** Both arms need
  cover, and the reuse arm must assert the token is the *existing* one rather
  than merely that a token came back.
- **`get_comm_flair_list:675-678`** tries `.first()` on an exact name/domain
  match and falls back to a case-insensitive `.one()`. The fallback raises where
  the first returns `None` — assert both.
- **`send_message` and `send_email` must be patched** by rebinding on
  `app.shared.community`; `from ... import` binds into the importing module's
  globals.

## Success criteria

- The `.get()` defect fixed at both sites, each pinned first and each pin
  inverted, with every named control still passing unchanged.
- Group D's five functions each at `missing_lines []` and `missing_branches []`
  **as measured by the full-suite `--cov=app` run**, with any line ruled
  unreachable carrying a **named** fact 75 cause and a proof, or an explicit
  statement that none fits. Fact 75 has **no cause 4(c)**; cause 6 is expressly
  *not* about a clause; cause 8 is narrow to a `try`/`except` whose body can
  never run.
- **No floor for `app/shared/community.py`** — 26 floors total, unchanged.
  Group B remains.
- No regression in the five closed modules: `post.py`, `reply.py`, `user.py`,
  `domain.py`, `site.py`.
- Full suite green; floors check chained with `&&` and **both** arguments
  against a `--cov=app` JSON.
- A mutation pass scoped to Group D's five functions, with both `.get()` fixes
  as mandatory mutations. Report the kill count **scoped to what was mutated**,
  as D602 does, and remember that **a crash kill is not a kill** unless a viable
  non-crashing variant also dies — sub-project 46 nearly banked one and had to
  add a test.
- Findings registered from **D625**; `tests/README.md` facts from **268**.
- **Exactly two production changes.**

## Environment

Unchanged and all still binding: no host Python with flask or pytest, so
everything runs through `./run_tests.sh`; coverage takes the dotted module form
and a path form silently collects nothing; coverage JSON is written outside the
repo and lands in the container's `/tmp`; `tests/check_coverage_floors.py` takes
two arguments and counts a floored module absent from the report as 0.0; only
the controller runs the full suite, one pytest session at a time, and a killed
pytest corrupts the test database — recover with `./run_tests.sh --down`.

**D610** records the harness baseline measured in sub-project 45: peak 745 MB
without coverage and 734 MB with, plateau ~685 MB, coverage costing about 18 MB,
286.98s without coverage and 368.61s with. If a run dies, read D610 before
assigning a cause — that entry exists because three causes were asserted before
anything was instrumented and the instrumentation contradicted all three.

**A known wart, left alone deliberately:** `tests/README.md` carries duplicate
fact numbers at 67, 100 and 124, predating this round. Renumbering would break
every citation that points at them by number, so they stay until a round scopes
that repair properly.

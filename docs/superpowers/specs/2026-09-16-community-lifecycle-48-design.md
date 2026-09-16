# Sub-project 48: community.py's lifecycle group — the last of the module

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `53a973b9`
**Predecessor:** sub-project 47, which closed the invite and flair group and took
`app/shared/community.py` from 53.791 to 72.103.

## Goal

Cover Group B — `make_community` and `edit_community` — which is the last
uncovered group in `app/shared/community.py`. When it closes, **the module takes
its first coverage floor**, withheld deliberately for four rounds because a floor
set earlier would have ratcheted against work not yet written.

**This round ships ZERO production changes.** It is the campaign's first pure
coverage round. Every defect found is registered with a proof, and the reasons
are given below rather than assumed.

## Targets

| function | line | missing statements | missing arcs |
|---|---|---|---|
| `edit_community` | `:293` | 85 | 36 |
| `make_community` | `:213` | 54 | 18 |
| **total** | | **139** | **54** |

**Derived, not carried forward.** Sub-project 47's delivered tree measured
`app/shared/community.py` at 72.10300 with **140 statements / 55 arcs** missing.
Subtract `comm_flair_ap_format`'s single proven-unreachable line and arc
(`[699]` / `[[698, 699]]`, established by algebraic proof in sub-project 47 with
no catalogued fact 75 cause fitting) and the remainder is exactly **139/54**.
That matches sub-project 47's own scoping figures for these two functions, which
nothing has touched since — its production footprint was two lines at `:130` and
`:196`.

139 statements is slightly above the campaign's 110-130 pace; 54 arcs sits inside
the 56-72 band at its lower edge. The round is one group, not two, and it is the
last, so splitting it would leave the module unfloored for another round.

## THE STRUCTURAL FACT THAT SHAPES THIS ROUND

**Both functions' web arms are unreachable from any production caller.** Verified
with `/usr/bin/grep -rn` over `app/`:

- **`make_community` has exactly one caller**: `app/api/alpha/utils/community.py:221`,
  with `SRC_API`. Its entire web arm, `:226-237`, is dead in production.
- **`edit_community` has exactly two callers**: `app/api/alpha/utils/community.py:261`
  (`SRC_API`, `from_scratch=False`) and `make_community:282` (`from_scratch=True`,
  passing `src` through). So its web arm `:306-317` is reachable only through
  `make_community`, and the combination **"web arm AND `from_scratch=False`"** —
  which contains the whole icon and banner deletion block at `:325-346` — is
  **unreachable in production entirely**.

**The web path is a separate implementation.** `app/community/routes.py:1216`'s
`community_edit` does not call the shared function. It sets fields on the model
directly and handles its own icon upload at `:1256-1260`.

**It has already diverged.** The route handles `private`, `topic`, `theme`,
`posting_warning`, `ai_generated`, `invitations`, `new_mods_wanted`,
`default_layout`, `default_post_type`, `downvote_accept_mode` and
`post_url_type`. The shared `edit_community` handles none of them.

This is **D612's contradiction at full scale** — two implementations of one
operation, guarded differently, drifted. Sub-project 46 repaired the small
version of this; here it is structural and a coverage round cannot fix it.

### How the round treats the dead arms

**Cover them, with the unreachability disclosed in every affected docstring.**
This is the campaign's established pattern for `SRC_PLD`-only branches
(`tests/test_shared_post_interactions.py:577` is the canonical example, and the
convention was reaffirmed across sub-projects 43, 45 and 47): reach the code by
calling the function directly with hand-built input, and say plainly in the
docstring that no production caller takes this path.

**Every such docstring must say so.** A test that reaches dead code without
disclosing it is how a later round comes to believe the path is live.

The alternative — proving the arms dead and setting the floor below 100 — was
considered and rejected. It would be a far larger unreachability claim than any
this campaign has made, and a wrong proof silently excuses real gaps rather than
surfacing them.

## Registered, not fixed — with the reasoning

**The budget is zero production changes**, and that is a decision rather than an
oversight. Each candidate below has a reason it is not being fixed in a coverage
round.

1. **`edit_community:322` is the only guard in the module using `is_admin()`.**
   Its four siblings — `delete_community:494`, `restore_community:523`,
   `add_mod_to_community:549`, `remove_mod_from_community:617` — all use
   `is_admin_or_staff()`. So **staff may delete a community, restore one, add
   moderators and remove moderators, but may not edit one.**
   **Why not fixed:** the fix *expands* permissions, granting staff a right they
   do not currently hold. That is a product decision about who may edit a
   community, not a coverage repair, and this campaign has never widened a
   permission. Registered for an owner to decide.

2. **`make_community:278` and `edit_community:378` dereference a `.first()`.**
   Both do `undetermined = Language.query.filter(Language.code == 'und').first()`
   and then `undetermined.id`. A missing row yields `AttributeError` on `None`.
   Same class as **D614** (`.get()`-then-dereference), in its `.first()` variant.
   **Why not fixed:** latent. `app/cli.py:181` seeds `Language(name='Undetermined',
   code='und')`, so a normally-initialised deployment always has the row. The
   failure needs an unseeded database. Registered with that precondition stated.

3. **`edit_community:322`'s first operand is probably subsumed.** Sub-project 46
   proved that `Community.is_owner(user)` implies `Community.is_moderator(user)`,
   because `models.py:740`'s `is_moderator()` tests membership in `moderators()`
   (`:716-722`), a list built from `is_owner OR is_moderator`, while `:747`'s
   `is_owner()` tests the column. `delete_community:494` carries the identical
   shape and the subsumption was registered there.
   **Why not fixed:** a subsumed disjunct is an equivalence, not a defect —
   fact 75 cause 3, proved algebraically. **The round must verify it holds here
   rather than assuming**, and if it does, no test can kill that operand and none
   should be written.

4. **`make_community:239` uses `is False` where the column is nullable.**
   `user.verified is False` does not fire when `verified` is `None`, and
   `app/models.py:981` declares `verified = db.Column(db.Boolean, default=False)`
   with no `nullable=False`. The default applies on INSERT, but `None` is
   representable, and such a user passes a guard meant to stop them.
   **Why not fixed:** the correct repair is arguably `not user.verified`, but
   that also changes behaviour for any row deliberately holding `None`, and
   establishing whether such rows exist is database archaeology beyond this
   round. Registered.

5. **`edit_community:341` and `:343` both call
   `cache.delete_memoized(Community.header_image, community)`.** When `:340` sets
   `image_id = None`, `:342`'s `if not community.image_id:` is then true and the
   call fires twice. The **icon** path at `:325-333` has no equivalent call at
   all. Registered as an asymmetry; neither half is a correctness defect.

6. **The parallel implementation** described above. Registered as the round's
   structural finding.

## Shapes the tests must handle

- **`make_community:214` and `edit_community:294` fork on `src`**, and the web
  arms are the dead ones. Both arms need cover; only the API arm is live.
- **`make_community:239`'s two-operand guard** needs each operand isolated.
- **`make_community:244-251`** does two existence checks with **different
  messages and different capitalisation** — `'A User with that name already
  exists, so it cannot be used for a Community'` at `:246` and `'community with
  that name already exists'` at `:251`. `:268`'s `IntegrityError` handler raises
  `'Community with that name already exists'` — same words, **capital C**.
  Assert the exact strings; a test matching case-insensitively cannot tell
  `:251` from `:268`.
- **`make_community:263-268` is a `try`/`except IntegrityError`.** Reaching `:268`
  needs a genuine integrity violation, not a mocked one. If it proves
  unreachable, that is **fact 75 cause 8** territory — but read cause 8's own
  text first: it is narrow to a `try`/`except` whose body can never run.
- **`edit_community:321`'s `from_scratch` fork** gates the entire
  `:325-346` block. Both arms need cover, and `from_scratch=True` is the only
  arm `make_community` reaches.
- **`edit_community:325-344`** is four nested conditionals over icon and banner
  state. This is where most of the 36 arcs live.
- **`:348` and `:354`** are three-operand conditions —
  `if icon_url and (from_scratch or icon_url_changed) and is_image_url(icon_url):`.
  Each operand needs isolation, and `from_scratch or icon_url_changed` is itself
  a disjunction inside a conjunction.
- **Patch by rebinding on `app.shared.community`** — `process_upload`,
  `make_image_sizes`, `is_image_url`, `task_selector`, `markdown_to_html`. A
  `from ... import` binds into the importing module's globals.
- **`cache.delete_memoized` mutants are unkillable** under `tests/conftest.py:68`'s
  `CACHE_TYPE = 'NullCache'` (D602, D589). Do not build assertions on them.

## THE METHOD THIS ROUND INHERITS

Sub-project 47 shipped **five** mechanism-(e) lockstep gaps across four tasks,
every one invisible to branch coverage and caught only by a hand-applied
conjoined mutant. Two method errors were found and corrected, and **both apply
directly here**, because `edit_community` has more independent conditions than
any function the campaign has covered:

1. **Key the decoupling table by BRANCH SITE, not condition name.** Three
   separate `apply` checks aggregated into one column looked decoupled and were
   not. `edit_community` has `from_scratch` read at `:321`, `:348`, `:354`,
   `:371` and `:388` — **five separate sites**, and they must be five columns.
2. **A row must record whether the outcome is OBSERVABLE, not merely whether the
   site was reached with a given value.** Seven tests reached a site in
   sub-project 47 and none could distinguish its arms, so all seven contributed
   nothing. A test that reaches `:348` but asserts nothing about `icon_id`
   contributes nothing to `:348`'s decoupling.

The final table must carry an observability column, and the mutation pass must
run an **exhaustive sweep** over {site} × {and, or} × {values}, as sub-project
47's fix round did to reach 36/36.

## Success criteria

- Both functions at `missing_lines []` and `missing_branches []`, measured on the
  full-suite `--cov=app` run, with any line ruled unreachable carrying a **named**
  fact 75 cause and a proof — or an explicit statement that none fits, per
  **fact 252**.
- **`app/shared/community.py` takes its FIRST coverage floor**, at its measured
  value. **27 floors total.** Record the measured percentage beside it.
- No regression in the five closed modules: `post.py`, `reply.py`, `user.py`,
  `domain.py`, `site.py`.
- Full suite green; floors check chained with `&&` and **both** arguments against
  a `--cov=app` JSON.
- A mutation pass over both functions, reported **scoped to what was mutated**
  as D602 does, with an exhaustive conjoined sweep and **zero survivors** or each
  survivor carrying a proof.
- Findings registered from **D640**; `tests/README.md` facts from **272**.
- **ZERO production changes.** `git diff --numstat <base> HEAD -- app/` must be
  empty.

## Environment

Unchanged and binding: no host Python with flask or pytest, so everything runs
through `./run_tests.sh`; coverage takes the dotted module form and a path form
silently collects nothing; JSON is written outside the repo and lands in the
container's `/tmp`; `tests/check_coverage_floors.py` takes two arguments and
counts a floored module absent from the report as 0.0; only the controller runs
the full suite, one pytest session at a time.

**D610** records the harness baseline. **The host has been heavily contended**:
sub-project 47 saw a full run take 673s against a 287-385s band, two background
runs watchdog-killed before collection, and one killed mid-test at 25% requiring
`./run_tests.sh --down`. **Read the output before assigning a cause** — a kill
before collection is harmless; a kill mid-test corrupts the database.

**A suite count is only true of the tree it ran on.** Sub-project 47's register
line went stale three times and sub-project 46's twice, every wrong value within
single digits of the truth. Take the figure **after the final review's fix
round**, not after the task the plan numbers last.

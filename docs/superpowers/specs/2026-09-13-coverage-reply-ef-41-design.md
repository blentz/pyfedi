# Coverage sub-project 41: `app/shared/reply.py` Groups E and F

**Date:** 2026-09-13
**Branch:** `blentz`
**Measured at:** `903dab20` (full suite, 4764 passed, 3 skipped, 336.65s, all 21 floors met)

## Goal

Take `app/shared/reply.py`'s moderator verbs and its four reply-only verbs to zero missing statements and zero missing branch arcs — **105 statements and 58 arcs** — and raise the module's floor.

Then, with the behaviour witnessed, **fix the authorization hole this scoping found in `choose_answer`/`unchoose_answer`** and invert the test that pins it.

## Measurement

Read from `summary.percent_covered` in the full-suite JSON at `903dab20`, and from coverage.py's own per-function table rather than from any figure in a document — **except for the module row's two totals, which were in fact copied from a document and were wrong; see the CORRECTION under the table.**

| | statements | missing | branches | missing | percent_covered |
|---|---|---|---|---|---|
| module | 371 | 231 | 194 | 126 | 36.8142 |

> **CORRECTED 2026-09-13 (sub-project 41, final fix wave; register D532).**
> **This row read `364 | 231 | 186 | 126 | 36.8142` from the day the spec was
> written until the final whole-branch review caught it, and it could not be
> true as written.** With 231 and 126 missing, 364 statements and 186 arcs give
> `(364-231 + 186-126)/(364+186) = 193/550 = 35.0909%`, not 36.8142. The
> percentage is the one figure in the row that was right: `36.8142% = 208/565 =
> (371-231 + 194-126)/(371+194)`.
>
> **The totals were the PRE-`903dab20` figures, and the percentage was the
> post-`903dab20` one** — the spec read the percentage from `903dab20`'s JSON
> as it says, and took the totals from an older table (`tests/README.md` fact
> 232's, where 364/186 *is* self-consistent with 32.909 and 35.091). Settled two
> ways that fail differently: by the arithmetic above, and by parsing all three
> revisions with `coverage.parser.PythonParser`, which returns **364/186 at
> `903dab20^`, 371/194 at `903dab20`, and 375/198 at HEAD**. The gap,
> **7 statements and 8 arcs**, is exactly D517's `'reversal'` arm in
> `vote_for_reply` — one `elif`, one assignment, three `if`s and two `return`s,
> and three branch points — added hours before this spec was written, by the
> same author, in the commit this spec names as its measurement point. **The
> binding authority's own headline measurement was an instance of the
> citation-staleness class the round went on to register, and it survived
> fifteen commits and nine task reviews because no instrument in this campaign
> reads a spec.**
>
> **NOTHING DOWNSTREAM CHANGES, and that was verified rather than assumed.**
> The per-group figures below (105/58 for E+F, 126/68 for B+D), the 231/126
> module totals, the floor of 66 and every number derived from them were
> re-checked by the final review and are unaffected: 21+21+24+15+15+9 = 105 and
> 71+55 = 126 still hold, and the round's measured HEAD figure of 375/198 is
> 371/194 plus exactly the four statements and four arcs this round's own
> production fix added. **The damage was confined to one number, and it is the
> number sub-project 42 will reach for first:** a round measuring
> `app/shared/reply.py` at HEAD sees **375** statements and, comparing against
> this row's old **364**, finds an unexplained gap of **11** where the true gap
> is **4** — and would then go looking for seven statements that were never
> missing. Against the corrected **371** the arithmetic closes on this round's
> own `+4` and nothing else.
>
> **Recorded as a CORRECTION rather than rewritten in place**, because a spec
> that quietly changes its mind is indistinguishable from one that was always
> right, and the next round needs to know this file was a victim of D532's class
> in order to distrust the rest of it appropriately.

| Group | Function | Stmts | Missing | Arcs | Missing |
|---|---|---|---|---|---|
| **E** | `mod_remove_reply` `:414` | 21 | 21 | 12 | 12 |
| **E** | `mod_restore_reply` `:450` | 21 | 21 | 12 | 12 |
| **F** | `lock_post_reply` `:486` | 24 | 24 | 14 | 14 |
| **F** | `set_collapse_post_reply` `:523` | 15 | 15 | 12 | 12 |
| **F** | `choose_answer` `:548` | 15 | 15 | 4 | 4 |
| **F** | `unchoose_answer` `:579` | 9 | 9 | 4 | 4 |
| | **this round** | | **105** | | **58** |
| B | `make_reply` `:156`, `edit_reply` `:216` | 71 | 71 | 34 | 34 |
| D | `report_reply` `:311` | 55 | 55 | 34 | 34 |
| | **sub-project 42** | | **126** | | **68** |

**Every one of the nine remaining functions has `missing == total`.** They are not partially covered; no test in the suite executes a single line of any of them. That is worth stating because it changes what a first task must do: there is no existing partial coverage to build on and no existing test to read for the conventions.

**Two figures in sub-project 40's spec were wrong and are corrected here.** `unchoose_answer` is **9** statements, not 8, so Group F is **63/34** and the four groups sum to **231/126** rather than 230/126. The group boundaries have also all shifted: D517's reversal-gate fix added lines to `vote_for_reply`, so every line number below was re-derived at `903dab20` and none was copied from the earlier plan.

## Why E and F together

105/58 is the campaign's demonstrated pace almost exactly.

The pairing is deliberate. **Group E has the strongest transfer available** — `mod_remove_reply` and `mod_restore_reply` are near-copies of `delete_reply` and `restore_reply`, which sub-project 40 closed, and their twins `mod_remove_post` (`app/shared/post.py:1057`) and `mod_restore_post` (`:1100`) were closed in sub-project 35. **Group F is the only group in the module with no precedent at all.** Pairing the cheapest with the riskiest gives the round slack if F surprises it.

It also leaves B and D — both heavily precedented — as a predictable final round that closes the module, rather than ending the module on its unknown.

## The harness

### What transfers unchanged from sub-project 40

Established in `tests/test_shared_reply_interactions.py` and not to be re-derived:

- `_seed_reply()` and its ordering constraints; `private=True` as the federation lever (D393(d)) that stops eager Celery task bodies at their first guard.
- **The `make_site()` rule**, in the form sub-project 40 corrected it to: a `Site` row is needed for `render_template` **or** for `can_downvote`, which reads `Site.query.get(1)` at `app/utils.py:2443` and dereferences it at `:2445` before any source fork.
- The SRC_API arm needs no request context: `get_ip_address` (`app/__init__.py:68-77`) catches `RuntimeError` and returns `''`.
- `authorise_api_user` requires `ap_id is None`, `verified` true, `banned` false.
- The raw-SQL `child_count` facts, including **D517's new guard shape** `if reply.path and len(reply.path) > 1:` at `:430` and `:465`.

### What is new, and what Task 1 must probe

- **`add_to_modlog` runs in four of the six functions** (`:437`, `:473`, `:506`, and not in the answer pair). `tests/README.md` fact **214** already records that it commits and raises on an unmapped action. All four action strings this round reaches — `delete_post_reply`, `restore_post_reply`, `lock_post_reply`, `unlock_post_reply` — **are** in `ModLog.action_map` (`app/models.py:3912-3929`, verified at source), so the raise at `app/utils.py:3568` is not in play. What **is** in play is `:3570`: `add_to_modlog` types the row `admin` or `mod` by reading the actor, and fact 214 already records that an id-1 actor types as `admin` because `is_admin()` short-circuits. A test asserting on `ModLog.type` must not use the seed's first user by accident.
- **`force_locale(get_recipient_language(...))` wraps the notification title** in `choose_answer:556`. `get_recipient_language` (`app/utils.py:4782-4795`) dereferences `db.session.query(User).get(user_id)` and then reads `language_id` and `interface_language`. Whether a factory user survives that path unmodified is **Task 1's first probe**.
- **`lock_post_reply:503` is a containment query**, `update post_reply set replies_enabled = :replies_enabled where path @> ARRAY[:parent_id]`, cascading the lock to every descendant. This is the module's only `@>` query and it reads the same `path` column D517 repaired. A test must seed a real descendant, or the cascade has no witness; and the interaction with a malformed imported path should be stated rather than assumed.
- **`choose_answer` writes a `Notification` and increments `unread_notifications`** (`:564-570`). That is the only notification write in either group.

## Four findings by reading, and their dispositions

### 1. `choose_answer`/`unchoose_answer` are unauthorized through the API — PIN, THEN FIX

Neither function contains any permission check. Both establish `user` and act.

The web route does guard. `app/post/routes.py:2443` and `:2453`:

```python
if current_user.is_authenticated and (current_user.is_admin_or_staff()
        or post_reply.user_id == current_user.id
        or post_reply.community.is_moderator()):
```

The API path does not. Traced end to end at source: `app/api/alpha/routes.py:983` calls `post_reply_mark_as_answer`, which is `app/api/alpha/utils/reply.py:687-697` — it reads `data`, calls `authorise_api_user(auth, return_type='dict')`, and dispatches to `choose_answer` or `unchoose_answer` on the value of `data['answer']`. `authorise_api_user` establishes **who the caller is**; it says nothing about what they may do. The route wrapper checks only `enable_api()`.

**So any authenticated API user can mark any comment on any post as the accepted answer, and unmark any.**

This is D500's family with the arms swapped: there the *web* arm lacked the check the API arm had, here the *API* arm lacks the check the web route has.

**Disposition: pin it with tests, then fix it in the same round, then invert the pinning test.** Sub-project 40 performed exactly this sequence across two sessions; this round does it once. The fix belongs at `app/api/alpha/utils/reply.py:687`, mirroring the web route's three-way guard, so that `choose_answer` stays a plain verb and the two entry points agree. The pinning test must carry its fix-edit obligation in its own docstring, per the amended unique-kill rule.

### 2. Two permission-disjunct divergences of identical shape — REGISTER

| Function | moderator | instance admin | `is_admin_or_staff()` |
|---|---|---|---|
| `mod_remove_reply:421` | yes | yes | **yes** |
| `mod_restore_reply:457` | yes | yes | **no** |
| `set_collapse_post_reply:531` | yes | yes | **yes** |
| `lock_post_reply:501` | yes | yes | **no** |

Staff who are not moderators can remove a comment but not restore it, and can set a comment collapsible but not lock it. The same missing disjunct, twice, in the same file.

**And a third divergence, against the twin module rather than within this one:** `lock_post:953` calls `post.community.is_admin_or_staff(user)` — a `Community` method — while `lock_post_reply:501` calls `post_reply.community.is_instance_admin(user)`. Not merely a missing disjunct: a different method. Whether the two should agree is the product question, so this stays registered with the other two.

**Registered, not fixed.** Which set is correct is a product decision, both arms are live behaviour rather than dead code, and this round already carries one production change. The round pins all four guards as they behave.

### 3. `lock_post_reply` and `set_collapse_post_reply` fail silently — PIN, THEN FIX

> **AMENDED 2026-09-13, after the spec was committed at `68622d12` and while the plan was being written.** This finding was first written as REGISTER, on the reading that the silent failure was the module's own design. Reading the post twins to source the plan's test code falsified that: **the repair already exists in this codebase, twice, and these two functions were left behind when it landed.** The disposition is changed to PIN-THEN-FIX and the round's production budget goes from one change to two. Corrected in place with this block rather than rewritten, because a spec that quietly changes its mind is indistinguishable from one that was always right (D394).

Neither has an `else`. A caller without permission gets no exception, no flash, and for `SRC_API` a 200 carrying the unchanged object at `:519-520` and `:544-545`. The return value cannot distinguish a refusal from a success.

**This is an UNPROPAGATED FIX, not a design.** `app/shared/post.py` carries the repair at two sites, both from PC2 in sub-project 36:

```
939  def lock_post(...)                      486  def lock_post_reply(...)
953      if is_moderator or is_admin_or_staff:    501      if is_moderator or is_instance_admin:
...          (body)                           ...          (body)
968      elif src == SRC_API:                     (no elif -- falls through)
969          raise Exception('Does not have permission')
```

`move_post:999-1000` carries the identical two lines. So the same unauthorized call raises on a post and returns a quiet 200 on a comment. `tests/test_shared_post_moderation.py`'s `test_an_unprivileged_user_changes_nothing` already pins the repaired shape with `pytest.raises(Exception, match='Does not have permission')`.

**Disposition: pin the silent behaviour, then add the two lines to `lock_post_reply` and `set_collapse_post_reply`, then invert the pins.** The edit is transcription — it exists verbatim twice in the twin module — not a design decision.

**ORDERING MATTERS FOR THE MUTATION PASS.** Before the propagation, the mandatory mutation neutralising `lock_post_reply:501` cannot crash: it silently passes, so only a state assertion kills it. After the propagation, the same mutant is killable by the raise as well. The pass must run **after** the fix, and the plan must say which of the two regimes each kill was measured in.

This also makes false-witness mechanism (a) acute for both functions while the pins are being written: **the return value is the same on both arms, so every pinning test must assert on state.**

### 4. `mod_remove_reply` and `delete_reply` disagree about which counters a removal moves — REGISTER

The divergence is inside the bot guard, and stating it that way matters for test design:

```
delete_reply                             mod_remove_reply
265  if not reply.author.bot:            427  if not reply.author.bot:
266      reply.post.reply_count -= 1     428      reply.post.reply_count -= 1
267      reply.post.reply_count_cross_posted -= 1      (no counterpart)
268      reply.community.post_reply_count -= 1         (no counterpart)
269  reply.author.post_reply_count -= 1  429  reply.author.post_reply_count -= 1
```

Both functions guard three-or-one counters on `not reply.author.bot` and move `author.post_reply_count` unconditionally. Inside that guard `delete_reply` moves **three** counters and `mod_remove_reply` moves **one**.

So a moderator removal and an author deletion leave the database in different states for the same disappearance. A test pinning this must seed the four counters at **distinct non-zero values** — sub-project 40's `_seed_distinct_reply_counters` already does exactly that, seeding 31/17/8/3 so they stay pairwise distinct after a move — or a mutant swapping two of them is invisible. Cross-references **D463** and sub-project 40's own mirror-divergence finding.

**Registered, not fixed**, and pinned in both directions so the divergence is witnessed by a test rather than asserted in a document.

## The five false-witness mechanisms

D451's four plus D469's fifth. Every test docstring names which branch it witnesses and why the assertion could only be produced by that branch.

1. **Asserting on state something else sets unconditionally.** Acute across this whole round: `lock_post_reply` and `set_collapse_post_reply` return the same value on both arms of their permission check (finding 3), and `mod_remove_reply:424-426` writes `deleted` and `deleted_by` on every path that gets past the guard.
2. **A fixture coincidence.** Acute for `add_to_modlog:3570`, where an id-1 actor types as `admin` whatever their real role.
3. **Emptiness with no same-mechanism positive control.** Acute for the two silent-failure functions: "nothing changed" is the refusal's signature *and* the signature of a broken fixture.
4. **An input taking the same path under both arms.**
5. **Two independent conditions exercised only in lockstep** cannot detect a swap between them. Acute for the three-disjunct guards, where `is_admin()` short-circuits True for user id 1 — sub-project 39 lost two tests to exactly this.

And: **a mutation's non-failures are evidence.**

## Verification

A mutation pass **scoped by the statement list, not the arc table** (D470), with **both the statement list and the compound list derived mechanically** via `ast.walk` rather than by reading (D487).

**`git diff --quiet -- app/` is the load-bearing tree check, not `wc -l`** (D488) — a same-line-count replacement is invisible to a line count, as sub-project 40 demonstrated live when a background scanner sampled a mutation window and reported it as a production defect.

**Restores must not use `git checkout -- app/` while this round holds uncommitted production changes.** Sub-project 41 plans a production fix, so for part of the round HEAD is not the correct baseline; that idiom silently reverted a real fix in sub-project 40's fix session. Use a reverse `sed` and verify the line.

**Two mandatory mutations beyond the statement list:**

- Neutralise `lock_post_reply:501`'s permission guard so it never refuses. **Run this after finding 3's propagation lands**, and say so: before the propagation the mutant cannot crash and only a state assertion kills it; after, the raise kills it too, and a pass that does not name which regime it measured has not measured anything.
- Neutralise `mod_restore_reply:457`'s guard likewise.

Four consecutive rounds have found a real hole this way, because every test supplied a permitted input.

Standing rules: a crash kill is not a kill unless a viable non-crashing variant also dies; an operator can be structurally void; an arc being equivalent does not make every mutation of its line equivalent; **fix-catching is not a unique kill**, and a test that closes no arc and no statement earns its place by a unique kill against a fault-direction mutant **or** by pinning a registered defect with the fix-edit obligation stated in the test itself.

## Production changes

**Two, named exactly.** Both land **after** the coverage tests pin today's behaviour, each with the failing observation pasted before the change and its pinning test inverted in the same commit.

1. **Finding 1** — an authorization guard at `app/api/alpha/utils/reply.py:687`, mirroring `app/post/routes.py:2443`.
2. **Finding 3** — `elif src == SRC_API: raise Exception('Does not have permission')` appended to `lock_post_reply` and `set_collapse_post_reply`, transcribed from `app/shared/post.py:968-969`.

No other production change is planned. Findings 2 and 4 are registered. **No defect will be manufactured to justify the round**, and if a further genuine one surfaces it is registered rather than folded in — this round has two production changes and their scope is named above.

## The documentation rule this round inherits

Sub-project 39 hit one defect seventeen times and sub-project 40 hit it at least six more, every one of those six **inside a correction written to fix a different instance of it**: a claim left standing after a change invalidated its premise. The worst case was 37 lines from its own correction, in one docstring, and survived three reviews.

**A claim is graded where it is read.** Every task re-reads each docstring it touches in full, before committing, and asks what premise its change invalidated elsewhere in that docstring — and sweeps the artefact in the same pass as the register.

**Re-derive an inherited premise with a second method that fails differently, not a second grep.** Sub-project 40's controller asserted "exactly two sites" from a grep hand-scoped to four path globs; a whole-tree search found four enforcement sites. Note also that the interactive `grep` in this environment is a `ugrep` wrapper carrying `--ignore-files`, which silently skips anything `.gitignore` excludes; `/usr/bin/grep` does not.

## Success criteria

- Groups E and F at zero missing statements and zero missing arcs, confirmed by re-measurement with **every arc checked as a pair**, and both lists checked **as lists** rather than by inspecting a global minimum and maximum (D475).
- The `app/shared/reply.py` floor raised to the measured `percent_covered`, rounded down.
- The `choose_answer`/`unchoose_answer` API authorization hole **closed**, with its pinning test inverted and its register entry marked fixed.
- PC2's refusal propagated to `lock_post_reply` and `set_collapse_post_reply`, with their pinning tests inverted, so an unauthorized API caller is refused rather than handed a 200 carrying the unchanged object.
- Full suite green, run by the controller in the foreground, unpiped, with the floors check chained by `&&` and given **both** arguments.
- Findings registered from **D519**, marker updated.
- `tests/README.md` facts from **232**, recording at minimum: what `force_locale(get_recipient_language(...))` needs from a factory user; how to witness `lock_post_reply`'s `@>` cascade; and the silent-failure shape of the two Group F permission guards.
- **Groups B and D re-measured** so sub-project 42 inherits a number rather than an estimate.

## Out of scope, carried forward

- **D499** — `vote_for_reply`'s web arm still has no voting-permission check while `vote_for_post`'s does.
- **D501** — the vote-quota off-by-one at four enforcement sites.
- **D518** — `Post.vote` no-ops where `PostReply.vote` raises, for a reversal with no existing vote.
- **D495** — 32 open mutation holes in `app/shared/post.py`, with recipes.
- The 17 open mutation survivors in Groups A and C, at shared-suite scope, with the lookup-scoping cluster (`:60` ×2, `:78`, `:100` ×2) ranked as the best first target.
- **D421, D442, D450, D463, D465, D476, D478, D490-D492.**
- Groups B and D of this module — 126 statements and 68 arcs before this round's side effects — are sub-project 42, which closes it.

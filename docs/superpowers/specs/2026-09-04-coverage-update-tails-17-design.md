# Sub-project 17: `update_post_from_activity`'s type tails

Sub-project 15's spec named this slice. Sub-project 14 covered the same
function's head and its mirrored halves and left the tails; sub-project 16 took
the post-side create and notify path instead. The tails are what remains, and
finishing them closes the last unfinished part of the create/update family the
campaign has been closing since sub-project 13.

## The unit

`update_post_from_activity` in `app/activitypub/util.py`, lines 3139-3566 —
extent derived from an unfiltered `^def ` scan, next `def` is `undo_vote` at
`:3567`.

Sub-project 14 took the function's head to **zero uncovered statements**. What
remains is 107 uncovered statements and 83 uncovered branches, in five clusters:

| cluster | lines | uncovered stmt | uncovered br |
|---|---|---|---|
| head: guards and the common edit | 3139-3272 | **0** | 2 |
| `type == 'Video'` — PeerTube vote totals over HTTP | 3273-3309 | 20 | 9 |
| `type == 'Question'` — the poll block | 3311-3360 | 38 | 25 |
| `type == 'Event'` and the attachment dispatch head | 3362-3423 | 5 | 9 |
| attachment tails, image, opengraph | 3424-3500 | 31 | 27 |
| tail: suspicious-domain notify and finish | 3501-3566 | 13 | 11 |

The counts are this spec's, measured from `scratch_full_cov.json` at commit
`c0cf0ed4`. **Re-derive them; do not inherit them.** Sub-project 16's plan
carried a family count that contradicted its own enumeration, and sub-project
15's plan under-counted a function's head guards.

### Why this unit

Three reasons, in order.

**It finishes a function the campaign has already split once.** Criterion 1 of
every sub-project since 13 has been stated against a finishable region, and the
campaign has learned that a region left half-covered is a region nobody returns
to on purpose. Sub-project 14 stopped at the tails deliberately; this is the
return.

**The harness is nearly all reuse.** `http_mock` exists at
`tests/conftest.py:288`. The image boundary — the thing that makes attachment
tests dangerous — is a solved problem twice over in this suite (see *Testing
approach*). Nothing here needs a technique the campaign has not already built.

**The poll block carries a real defect on a peer-reachable path**, described
below, and the correct spelling for it sits twenty lines above the defect.

## The asymmetry this slice is built around

### Three loops over one peer-supplied list, three different guard levels

The `type == 'Question'` block reads `request_json['object']['oneOf']` or
`['anyOf']` into `votes` (`:3314-3318`) and then iterates that same list three
times.

**The counting loop** (`:3323-3331`) guards every read:

```python
            for vote in votes:
                if not 'name' in vote:
                    continue
                if not 'replies' in vote:
                    continue
                if not 'totalItems' in vote['replies']:
                    continue

                total_vote_count += vote['replies']['totalItems']
```

**The Edit-recreate loop** (`:3346-3349`) guards nothing:

```python
                    i = 1
                    for vote in votes:
                        new_choice = PollChoice(post_id=post.id, choice_text=vote['name'], sort_order=i)
                        db.session.add(new_choice)
                        i += 1
```

**The totals loop** (`:3354-3357`) guards nothing, twice over:

```python
            for vote in votes:
                choice = PollChoice.query.filter_by(post_id=post.id, choice_text=vote['name']).first()
                if choice:
                    choice.num_votes = vote['replies']['totalItems']
```

Two things make this sharper than an ordinary missing guard.

**The counting loop's `continue`s are what select the Edit branch.** `:3333` is
`if total_vote_count == 0:  # Edit, not a totals update`. A vote that lacks
`name` contributes nothing to the count, so a poll whose votes *all* lack `name`
produces `total_vote_count == 0`, takes the Edit branch, and reaches
`vote['name']` at `:3347` — the very key whose absence routed it there.

**The totals loop iterates every vote, not the well-formed ones.** One
well-formed vote makes `total_vote_count > 0`; a malformed sibling in the same
list then raises at `:3355` or `:3357`. The counting loop's `continue` hides the
malformed entry from the count and leaves it in the list.

Both loops are downstream of `log_incoming_ap` and reachable from a peer
document. This is the unguarded-peer-input family the register indexes — now
seventeen members, twelve open.

### A second, quieter one

`poll.end_poll = request_json['object']['endTime']` (`:3338`) assigns a raw
peer-supplied string to a `DateTime` column, guarded only by
`if not 'endTime' in request_json['object']: return` (`:3336-3337`) — a
membership test, not a type or format test. The Event block twenty-six lines
below does the same reads through `datetime.fromisoformat`
(`:3367-3368`), which is the codebase's own spelling for peer-supplied
timestamps. **Establish from source whether the twin at `:3367` is itself a
register entry** before treating it as the correct spelling; the campaign's fix
rule has a second corollary for exactly this.

## Goal

Cover the tails, fix what clears the mechanical-fix bar, register the rest, and
close two findings sub-project 16 parked.

## Out of scope

- **`make_image_sizes_async`** (`app/activitypub/util.py:1733-1986`, 152
  uncovered statements). The attachment cluster *calls* `make_image_sizes`
  (`:3496`, and `:3389` in the Event block), and under this harness that
  executes. Tests must stop it at its boundary, not cover it. It is the largest
  region left in the file and belongs to its own sub-project.
- **`undo_vote`** (`:3567-3602`), the next function down. Adjacent, not in scope.
- **D274**, the NULL `unread_notifications` hazard. Registered by sub-project 16
  with 49 executable crash sites and 7 silent SQL sites; fixing it means choosing
  behaviour the codebase has never had, which is a spec of its own.
- **The prior fixes' reasoning already inline in this region.** `:3396-3409`
  (the Event `new_url` sentinel), `:3444-3451` (the `url_is_parseable` guard),
  and `:3544-3555` (`post.url = None` rather than `''`) are earlier
  sub-projects' fixes carrying their own arguments. Do not re-litigate them.
  Cover the code; leave the comments alone unless this slice falsifies one.

## The defects this slice must confront

Every item here is a candidate, not a verdict. The plan decides fix-versus-
register by the campaign's rule, and each decision is argued from source.

### Authorised for fixing

**The two unguarded vote loops.** The correct spelling exists in the same block
(`:3324-3325`), which clears the mechanical-fix bar for the `'name'` reads. Note
that the Edit loop's repair is not purely mechanical in one respect: `i`
increments per iteration (`:3349`), so skipping a malformed vote changes the
`sort_order` of every choice after it. The plan must decide whether `continue`
before or after the increment is the correct spelling, and argue it — that is a
behaviour question hiding inside a mechanical-looking fix, and it is exactly the
shape the fix rule exists to catch.

**The totals loop's `vote['replies']['totalItems']` read** (`:3357`) needs both
guards the counting loop applies, not just the `'name'` one.

### Registered by default; the plan may propose fixes

- **`poll.end_poll`'s raw string assignment** (`:3338`). Choosing a parse
  behaviour — reject the Update, coerce, or store as-is — is new behaviour.
- **The Event block's thirteen consecutive unguarded subscripts**
  (`:3367-3379`, one per line). `startTime`, `endTime`, `timezone`,
  `maximumAttendeeCapacity`, `participantCount`, `onlineLink`, `joinMode`,
  `externalParticipationUrl`, `anonymousParticipation`, `isOnline`,
  `buyTicketsLink`, `feeCurrency`, `feeAmount` — each a `KeyError` on a peer
  document that omits it. Mostly covered code, so this is a register finding
  the coverage work surfaces rather than one it closes.
- **The duplicated `targets_data` literal** in the suspicious-domain tail
  (`:3505-3510` and `:3523-3528`, byte-identical five-key dicts). This is the
  D242/D262 duplication family's shape at a fourth site. **Check the register
  before writing a new entry** — the family index exists because this campaign
  has twice registered a defect whose sibling was already registered.

## Testing approach

**Entry is a direct call**, as in every prior slice of this family.

**The image boundary is solved twice already; pick one and say why.** Under this
harness `make_image_sizes` executes rather than enqueues — `tests/conftest.py`
sets `task_always_eager=True`, and `make_image_sizes` (`:1724-1729`) dispatches
inline under `current_app.debug` and via `.apply_async` otherwise, so both arms
run the 152-statement body. The two established techniques:

1. **Serve a 404** so `make_image_sizes_async` stops at its `get_request`, whose
   bare `except:` swallows the failure. Used by
   `tests/test_ap_actor_json_person.py:898-908`; the bare-except behaviour is
   already harness fact at `tests/README.md:1091`.
2. **Turn off `cache_remote_images_locally`.** Used by
   `tests/test_event_post_type_survives_update.py:161`. Note this only guards
   the Event block's call at `:3389`, which is gated on the setting — the
   attachment cluster's call at `:3496` is **not** gated, so technique 2 alone
   does not cover the attachment path. Read both call sites before choosing.

**`http_mock`** (`tests/conftest.py:288`) serves the `type == 'Video'` block's
`likes` and `dislikes` fetches and the `opengraph_parse` call at `:3483`. The
Video block has a retry: `get_request`, `except httpx.HTTPError`, `time.sleep(3)`,
retry, `except httpx.HTTPError` again (`:3280-3287`). **A test that exercises the
retry pays three real seconds** unless the sleep is neutralised; decide how, and
note that a test asserting the retry happened must distinguish it from the
first attempt succeeding.

**Assertions are on persisted row state.** The poll block's observable effects
are `PollChoice` rows, their `choice_text`, `sort_order` and `num_votes`, and
`Poll.end_poll` / `Poll.mode`. The Edit path issues raw
`DELETE FROM "poll_choice_vote"` and `DELETE FROM "poll_choice"` (`:3341-3343`)
before recreating, so a test must seed rows that would survive a no-op and prove
they did not.

**Primary-key collisions make assertions vacuous.** `tests/conftest.py:143`
truncates with `RESTART IDENTITY`, so ids restart at 1 in every test — harness
fact 89. Sub-project 16 shipped an assertion mutable to a sibling id with the
whole file green, and its repair needed *three* extra rows before the ids were
pairwise distinct. Any assertion on an id-valued field needs distinct seeds and
an explicit guard.

**Coverage cannot see a conditional expression** — harness fact 87. Enumerate
every `x if y else z` in the region **by AST walk, not by grep**: fact 94, and
sub-project 16's reviewer used `ast.parse` → `IfExp` inside the `FunctionDef`
precisely because a textual search misses ternaries in dict literals, f-strings,
comprehensions and argument defaults. `:3410`, `:3452` and `:3501` are three
this spec found by eye — the first two are the same
`old_url if post.type == POST_TYPE_EVENT else None` expression, the third is
`domain_from_url(old_url) if old_url else None`. **That is a floor, not a
count**: the plan reports its own, derived by walk.

**A registered defect is not necessarily observable.** Sub-project 16's scoped
defect turned out latent — no test could distinguish fixed from unfixed code,
and the fix landed under an equivalence argument instead. Before planning a
test that proves a fix, establish that the defect has an observable difference.

## New test file

`tests/test_ap_update_post_tails.py`. Not an extension of sub-project 14's
`tests/test_ap_update_pair.py`, which is already 2545 lines and 108 tests and
whose harness is pure DB where this slice's is respx plus an image boundary.

## Global constraints

- Defects inside `update_post_from_activity` are fixed test-first, each in its
  own commit, separate from every test-only commit, and each proved by a
  mutation that fails a named test — **unless the defect is latent**, in which
  case the fix lands under an equivalence argument and a re-run of the
  accumulated mutation tables, and the plan says so.
- A defect is fixed when the correct spelling already exists in the file and the
  change is mechanical; it is registered when the fix would require choosing new
  behaviour for a case the codebase has never handled. Before ruling
  register-only for want of a correct spelling, grep the file for the twin —
  and check the register for whether that twin is itself a D-entry.
- Findings are numbered from **D284**, and **all** live "Next free number" notes
  are updated in the same change. Sub-project 16 found five live notes where the
  prior count said four; re-run the mechanical test rather than inheriting a
  number.
- Locate every code target by content, not by the line numbers in this spec.
- Verify a citation's function attribution separately from its line range, and
  derive a function's extent from an unfiltered `^def ` scan.
- **Read line numbers with `grep -n`, `awk` on `NR`, or a pipe through `cat -n`
  — never by counting lines out of a bare `sed -n 'A,Bp'`**, which prints no
  numbers and is off by one whenever the range opens on a blank line. Harness
  fact 95, and the mechanism behind the one citation error in sub-project 16
  that reached a committed file.
- Docstrings must be true. Prefer quoting production code to describing it. A
  citation that quotes N lines cites those N lines, not the enclosing function's
  extent.
- Every test asserts on persisted row state, never merely that nothing raised.
- No vacuous assertions: never assert a value equal to a column's declared
  default without seeding a contrary baseline first.
- The full suite must pass. **Only the controller runs it**, one session at a
  time, and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## Also closed by this sub-project

Two findings sub-project 16 parked at its cap, both one-line repairs in files
this slice's register task already opens:

- `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md:5613` states
  the nine `author` hits in `app/activitypub/util.py:2804-2936` include "five
  `author_id=` column keywords". There are four — `:2887` is
  `'author_id': post.user_id}`, a dict key in the `NOTIF_TOPIC` arm. The total
  of nine and D283's verdict are unaffected. **Verify before repairing**; the
  cell may have drifted since.
- `tests/test_ap_notify_post.py:410`'s distinctness guard omits
  `post.instance_id`. Mutant P-M3 dies today only because
  `post.instance_id == 1 == community.id`. Add it to the guarded set.

## Success criteria

1. **Zero uncovered statements and zero uncovered branches** in
   `update_post_from_activity`, or a named catalogued cause for each remainder.
   Sub-project 16 reached zero on its unit; this unit's head is already there,
   so the whole function reaching zero is the bar.
2. Every guard and conjunct in the region mutation-tested one at a time, each
   kill attributed to a distinct named test, recorded with kill type
   (assertion or crash) and sole-or-multi.
3. Every conditional expression in the region has **both** arms exercised or a
   stated catalogued cause — enumerated by AST walk, with the count reported and
   any disagreement with this spec stated plainly.
4. The three vote loops' guard asymmetry resolved: fixed where the rule allows,
   registered where it does not, with the `sort_order` question argued either way.
5. Every fix proved by a mutation that fails a named test, or by an equivalence
   argument plus a mutation re-run if the defect proves latent.
6. Findings registered from D284, house style matched, every live "Next free
   number" note advanced, no existing entry renumbered, moved or edited.
7. The two parked sub-project 16 residuals closed.
8. Harness facts added for anything that generalises, with dropped candidates
   named and justified.
9. `coverage_floors.ini` for `app/activitypub/util.py` raised from **72** to the
   measured blended figure rounded down.
10. Full suite green, one pytest session at a time, run by the controller.

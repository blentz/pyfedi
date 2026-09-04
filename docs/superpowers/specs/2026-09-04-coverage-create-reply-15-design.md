# Sub-project 15: the create path's reply half

**Date:** 2026-09-04
**Module:** `app/activitypub/util.py` — `create_post_reply`,
`notify_about_post_reply`
**Predecessors:** 5a-7 covered the inbox dispatcher; 8 webfinger; 9 the actor
profiles; 10 the collections; 11 the content objects; 12 the moderation and
ban-removal cluster; 13 the refresh-profile trio; 14 the update pair's
mirrored core. 8-11 took `app/activitypub/routes.py` from 35% to 90.8%; 12
through 14 took `util.py` from 48.9% to **66.8156%**.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D260**

## The unit

| Function | Lines at writing | Uncovered |
|---|---|---|
| `create_post_reply` | 2593-2771 | **83** |
| `notify_about_post_reply` | 2936-3005 | **26** |

**109 uncovered statements.**

`create_post_reply` builds a `PostReply` from the document a remote peer sends
in a `Create`. `notify_about_post_reply` is the notification fan-out it feeds.
Both are reachable by a peer choosing what to send.

Both spans above were taken from an **unfiltered** scan of `^def ` across the
range. A filtered grep returns the next *matching* def rather than the next
def, which is how this campaign drifted one function's extent three times.

### Why this unit and not the type tails

Sub-project 14's spec named `update_post_from_activity`'s type tails as its
successor. **That recommendation is superseded, and this spec says why so the
change is not read as drift.**

`create_post_reply` is the **create-path mirror of the function sub-project 14
just covered in full**. Same `source`/markdown arm, same language resolution,
an attachment loop of the same shape, the same Mention block with the same
four suppression rules, the same `ids = tuple(ids)`, the same `force_locale`.
The type tails, by sub-project 14's own words, are "none of it mirrored".

The comparison between siblings is what has found every defect in this
campaign since sub-project 9. Applied here it has a **freshly-proven** sibling
to compare against — one whose every branch was mutation-tested a slice ago —
and it has already produced three findings without anyone looking for them.

The type tails remain a coherent slice and become **sub-project 16**, carrying
D245 with them.

## The asymmetries — this slice's whole point

Verified against source while scoping. Line numbers are navigational aids;
locate by content.

| Axis | `create_post_reply` | `update_post_reply_from_activity` |
|---|---|---|
| `content` is `null` | **unguarded** — `.startswith` on `None` | guarded, fixed by SP14 (`e9c38153`) |
| `find_language_or_create(...).id` | **reads the unflushed `.id`** | fixed by SP14 (`a7d40a3e`) |
| empty-tuple `IN ()` | **unguarded** — this is D243 | fixed by SP14 (`04386275`) |
| rule 3's `continue` placement | **correct** — the sibling SP14 copied from | was botched; fixed (`0df51342`) |
| `contentMap` language fallback | **present** | absent |
| no-language fallback | **`site_language_id()`** | absent — leaves it unchanged |
| Mention handling | two-phase: collect `local_users_to_notify`, then notify | notifies inline |
| `distinguished` | ternary, defaults `False` | bare `if`, no default |
| `repliesEnabled` | absent | present |
| `source['mediaType']` | guarded | guarded — the one row that already agrees |
| tail handler | `except Exception as ex` | none |

**The first three rows are the same defects, unfixed, in the create path.**
Each now has a correct spelling in the same file, in the function sub-project
14 repaired. That is exactly the shape this campaign's fix rule was written
for.

**The fourth row is why the create path is the authority here**, not the
update path: sub-project 14's Fix F re-indented the update copy to match
*this* function. When the two disagree, read this one first.

## Goal

Full statement coverage **of the scoped region** — both functions entire —
and branch coverage sufficient that no guard in them survives having any one
of its conjuncts dropped. Expected effect: `app/activitypub/util.py` moves
from its measured **66.8156%** blended upward, and the floor rises from 66 to
the measured figure rounded down.

**The criterion is stated against a region that can be finished**, which is
what made sub-project 14 the first slice in this campaign to meet it.
Sub-project 13 stated the same criterion against whole functions, missed it by
124 statements, and only discovered that at its final review.

## Out of scope

- **`update_post_from_activity`'s type tails** — Video vote collections,
  Question poll rebuild, Event fields, the Link/Document/Audio/Image
  attachment walk, `POST_TYPE` detection with `make_image_sizes`, the
  moderator and admin notifications, cross-post recalculation. **Sub-project
  16**, carrying D245.
- The rest of `app/activitypub/util.py`: `make_image_sizes_async` (152
  uncovered), `process_report` (76), `new_instance_profile_task` (66),
  `notify_about_post_task` (48), `undo_vote` (28).
- **The refresh trio's residual 124 statements**, registered as D235 and still
  open.
- `create_post` (`:2774`), the sibling of `create_post_reply` on the post
  side. Named here because a reader will ask: it is a third copy of some of
  this material, and taking it would make this slice a trio rather than a
  pair. A later slice may pair it with `update_post_from_activity`'s core.
- `find_reply_parent`, `PostReply.new`, `notification_subscribers`,
  `find_language`, `find_language_or_create`, `markdown_to_html`,
  `allowlist_html`, `html_to_text`, `blocked_users` and
  `get_recipient_language` are driven or doubled here, not tested here.

## The defects this slice must confront

Same bounded fix authorisation sub-projects 5c through 14 carried: **defects
found inside these two functions are fixed test-first, each in its own commit,
separate from every test-only commit, and each proved by a mutation that fails
a named test.** Anything larger is registered.

The rule sub-projects 13 and 14 refined and this slice inherits: **a defect is
fixed when the correct spelling already exists in the file and the change is
mechanical; it is registered when the fix would require choosing new behaviour
for a case the codebase has never handled.** Sub-project 14 added a corollary
the hard way: **before ruling a defect register-only for want of a correct
spelling, grep the file for the twin** — a mirrored pair is not the only place
a sibling can live.

### Authorised for fixing

**`create_post_reply` crashes on `"content": null`.** It calls `.startswith`
on the value where its twin guards `is not None`. `AttributeError`, remotely
triggerable, and the correct spelling is in the function sub-project 14 fixed.

**`create_post_reply` reads an unflushed `Language.id`.**
`find_language_or_create` adds without flushing and the app factory sets
`autoflush=False`, so `.id` is `None` and the new reply is created with no
language. Sub-project 14 fixed the same read in both update functions by
assigning through the relationship; here the id is passed to `PostReply.new`,
so **the repair may need a different shape** — read `PostReply.new`'s
signature before deciding, and if the mechanical fix is not available, say so
and register instead of inventing one.

**D243 — the empty-tuple `IN ()`.** `ids = tuple(ids)` followed by
`SELECT user_id FROM "post_reply" WHERE id IN :ids`; psycopg2 renders `IN ()`
and Postgres rejects it. Sub-project 14 fixed the update-path twin with an
`if ids:` guard and registered this one because it could not prove a fix in a
function it had no tests for. This slice has those tests.

### Carried forward and fixed here

**D257 — `datetime.fromisoformat` is wrapped in `except ValueError`, but a
non-string `updated` raises `TypeError`, which escapes.** Two sites, one token
each: `except (ValueError, TypeError)`. It is the only one of sub-project 14's
five late crashes whose intended behaviour the file already states — the
`except` clause directly below it says what should happen. Both sites are in
`update_post_from_activity` and `update_post_reply_from_activity`, outside
this slice's unit, so **this fix is authorised by name rather than by the
bounded rule**, and its pin goes in `tests/test_ap_update_pair.py`.

### Registered by default; the plan may propose fixes

- **Only `create_post_reply` falls back to `site_language_id()`** when a
  document carries no language at all. Its twin leaves the existing language
  untouched. Which is right depends on whether a reply's language should
  default to the instance's — a decision, not a guard.
- **Only `create_post_reply` collects Mentions in two phases**, building
  `local_users_to_notify` before notifying. Its twin notifies inline. The
  two-phase shape is why this copy's rule 3 works and the update copy's did
  not.
- **`distinguished` defaults differ**: a ternary with `False` here, a bare
  `if` in the twin, so an absent key leaves the twin's value unchanged and
  sets this one to `False`.
- **`create_post_reply` has a bare `except Exception as ex` tail** that its
  twin lacks. What it swallows is worth recording before anything is changed.
- **`contentMap` is now a three-way asymmetry**, not the pair asymmetry
  sub-project 14 registered as D250: create-reply has it, update-reply lacks
  it, update-post has it. The register entry should be corrected by the
  file's marked-correction convention rather than rewritten.

## Testing approach

**Entry: direct function call.** `create_post_reply(store_ap_json, community,
in_reply_to, request_json, user, announce_id=None)` is a wider surface than
sub-project 14's two-argument functions. **The plan reads `find_reply_parent`
and `PostReply.new` before writing any seed**, since `in_reply_to` is resolved
by string content — `'comment'` and `'post'` in the URI are the hints — and
`PostReply.new` does work the tests will observe.

**`redis_lock_only_double` is required**, not `redis_double`. This
environment's fakeredis has no Lua scripting and redis-py's `Lock.release()`
issues an `EVALSHA`. `notify_about_post_reply` opens two `redis_client.lock`
blocks; `create_post_reply` reaches them through `PostReply.new`. The double
lives in `tests/test_inbox_dispatch_votes.py` and is reused, not reinvented.

**`db.session` is the session under test**, as in sub-project 14, with
`expire_on_commit` at its default `True`.

**Every test asserts on persisted row state** — a `PostReply`, a
`Notification`, a `UserFlair`, or a count — never merely on the absence of an
exception. Both functions commit as they go, so a guard that abandoned the
whole document would pass a bare "nothing raised" test.

**No vacuous assertions.** Seed the opposite of any declared default before
asserting it.

**Mutation discipline.** Every guard mutation-tested with each conjunct
dropped separately, each killed by a distinct named test. The traps this
campaign has paid for, all of which apply here:

- **`in` against a string is a substring test**, not a membership error, so a
  "wrong type" fixture chosen as a string sails through a downstream
  membership conjunct that would have raised on any other type.
- **A test that reaches a guard's False side naturally cannot kill a mutant
  that forces that guard False.**
- **A negative test can fail to kill its guard because a later step
  independently produces the same outcome.**
- **An assertion on a value a later normalisation step would produce anyway
  cannot kill the arm that produced it early.**
- **Adding a conjunct can unkill an existing test** — re-run a guard's
  existing mutations after changing it, not only the new one.
- **Mutating a whole guard to `if True:` is a site-level proof, not a
  conjunct-level one.**
- **Five causes of a genuinely unkillable clause** are catalogued: a fixture
  that never creates the excluded row; subsumption by a later conjunct;
  production exception handling swallowing the effect; unreachable data; and
  tautology. Name the cause; never invent a test to fake a kill.

**Docstrings must be true.** Sub-project 14 shipped five false ones, every one
a prose claim about production structure that no grep could catch, and one
drifted quoted code block. **Do not state a position, ordering, distance or
count in prose unless you have just read it.** A quoted code block is the one
kind of claim that can be audited mechanically, so prefer quoting to
describing.

**One pytest session at a time.** Any run over 600s is erroneous. The suite
produces **shifting false failures on a stale stack** — `IntegrityError:
duplicate key ... "ix_instance_domain"` during seeding, with a different test
failing each run. Any surprising failure gets `./run_tests.sh --down` and a
re-run before it is believed.

## New test file

One new file, `tests/test_ap_create_reply.py`. D257's pin is the exception: it
goes in `tests/test_ap_update_pair.py`, beside the guard it corrects.

## What coverage cannot prove

Sub-project 14 met full statement coverage of its region **and still shipped
nine unguarded peer subscripts**, because each is an input shape no fixture
sends. Full statement coverage of a region says nothing about the space of
documents a peer can send into it.

**This slice states that limit up front rather than discovering it at the
final review.** Meeting criterion 1 is necessary and not sufficient; the
asymmetry table, not the coverage figure, is what finds defects here.

## Global constraints

- Defects found in these two functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered, except
  D257, authorised by name.
- Findings are numbered from **D260**, and **all** live "Next free number"
  notes are updated in the same change. The historical notes are frozen
  records — leave them alone.
- The coverage floor for `app/activitypub/util.py` rises to the measured
  blended figure rounded down. It currently reads 66.
- Locate every code target by content, not by the line numbers in this spec.
- **Verify a citation's function attribution separately from its line range.**
  A citation of the form "*code* inside *function* (`:N`)" carries two claims
  and needs two checks. This campaign has shipped false attributions whose
  line numbers were correct, three times — and one of those was itself written
  as a correction. **Correcting a claim does not correct its copies:** grep
  for the wrong claim, not only for the right one.
- The full suite must pass. Only the controller runs it, one session at a
  time, and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## Success criteria

1. Full statement coverage of both functions, **measured per-region from the
   coverage JSON and reported**, not assumed.
2. No guard survives any one conjunct being dropped, each kill by a distinct
   named test, with any unkillable clause named and its cause given.
3. Every test asserts on persisted row state, not merely on the absence of an
   exception.
4. The three mirrored crash paths — the null `content`, the unflushed
   `Language.id`, and D243's empty-tuple `IN ()` — are pinned, then fixed or
   registered with a stated reason, each with a witnessed pre-fix failure and
   a mutation proof.
5. D257 is fixed at both sites, pinned in `tests/test_ap_update_pair.py`.
6. Every asymmetry in the table above is either fixed with a mutation-proved
   test or registered with a stated reason, and D250 is corrected to the
   three-way form by the file's marked-correction convention.
7. `coverage_floors.ini` raised for `app/activitypub/util.py`.
8. The findings register carries every defect found, from D260.
9. Full suite green.

# Sub-project 16: the post-side create and notify path

**Date:** 2026-09-04
**Module:** `app/activitypub/util.py` — `create_post`, `notify_about_post`,
`notify_about_post_task`
**Predecessors:** 5a-7 covered the inbox dispatcher; 8 webfinger; 9 the actor
profiles; 10 the collections; 11 the content objects; 12 the moderation and
ban-removal cluster; 13 the refresh-profile trio; 14 the update pair's mirrored
core; 15 the create path's reply half. 8-11 took `app/activitypub/routes.py`
from 35% to 90.8%; 12 through 15 took `util.py` from 48.9% to **71.0362%**.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D274**

## The unit

Spans taken from an **unfiltered** scan of `^def ` across the range. A filtered
grep returns the next *matching* def rather than the next def, which is how
this campaign drifted one function's extent three times.

| Function | Lines at writing | Uncovered |
|---|---|---|
| `create_post` | 2777-2793 | **5** |
| `notify_about_post` | 2796-2803 | 0 |
| `notify_about_post_task` | 2804-2936 | **48** |

**53 uncovered statements.**

`create_post` builds a `Post` from the document a peer sends in a `Create`;
`notify_about_post` is its two-line dispatcher; `notify_about_post_task` is the
notification fan-out. Every branch is reachable by a peer posting, or by a
local user's subscription and block settings.

`notify_about_post` has no uncovered statements and is in scope only because it
is the two-line bridge between the other two — testing the pair without it
would leave a reader asking why.

### Why this unit

`notify_about_post_task` is the **post-side twin of
`notify_about_post_reply`**, which sub-project 15 covered to zero one slice
ago. Same job — fan a new object out to everyone subscribed — and a
freshly-proven sibling to compare against.

**One correction to the record.** Sub-project 15's final review named
`create_post` as "the third copy of the create/update reply material". It is
not: it is a 17-line wrapper — `local_only`, visibility, then `Post.new(...)`
inside a tail `except`. The body, language, attachment and mention material
lives in `Post.new` (`app/models.py`), not here. That candidate dissolves, and
this spec records the correction so nobody re-derives it.

## The asymmetries — this slice's whole point

Verified against source while scoping. Line numbers are navigational aids;
locate by content.

### Four arms, three different filter sets

`notify_about_post_task` fans out through four subscription types in sequence,
each a near-copy of the last, each guarded by a different set of block checks:

| arm | `blocked_users` | `blocked_communities` | `blocked_or_banned_instances` |
|---|---|---|---|
| `NOTIF_USER` | **absent** | present | present |
| `NOTIF_COMMUNITY` | present | **absent** | present |
| `NOTIF_TOPIC` | present | present | present |
| `NOTIF_FEED` | present | present | present |

Two of four carry all three checks; the other two each omit a different one.
Both omissions have a plausible reading — you are unlikely to be blocking a
user you subscribed to, or a community you follow — but "plausible" is what
the register exists to record, and a subscription made before a block is
exactly the case nobody tested.

### The `NOTIF_FEED` arm marks recipients it never notified

In the `NOTIF_USER`, `NOTIF_COMMUNITY` and `NOTIF_TOPIC` arms,
`notifications_sent_to.add(notify_id)` sits **inside** the `if` that decides
whether to notify. In the `NOTIF_FEED` arm it sits one level out, in the
`for notify_id` body, so it runs **even when the recipient was filtered out**.

The set is then consulted by every arm's `notify_id not in
notifications_sent_to` check — and the feed arm's own loop runs once per feed.
So a user subscribed to two feeds, filtered out of the first for any reason,
is recorded as already-notified and **silently skipped for the second**.

Three of four right, the correct spelling present three times in the same
function, and the repair is one indentation level.

### Against the reply-side twin

| Axis | `notify_about_post_reply` (covered by SP15) | `notify_about_post_task` |
|---|---|---|
| session | `db.session` | `get_task_session()` under `patch_db_session` |
| entry | called directly | a Celery task, dispatched by `notify_about_post` |
| subscription types | one per branch | four in sequence |
| recipient filters | author-excluded only | up to three block checks per arm |
| cross-type de-duplication | none | a `notifications_sent_to` set |
| unread counter | increments in one branch, **recounts** in the other | increments in all four arms |
| commit granularity | per branch | **per recipient**, inside each loop |
| failure handling | none | `except Exception: session.rollback(); raise`, then `finally: session.close()` |

## Goal

Full statement coverage **of the scoped region** — all three functions — and
branch coverage sufficient that no guard in them survives having any one of its
conjuncts dropped. Expected effect: `app/activitypub/util.py` moves from its
measured **71.0362%** blended upward, and the floor rises from 71 to the
measured figure rounded down.

**The criterion is stated against a region that can be finished.** Sub-project
14 was the first slice to meet it; sub-project 15 met a stronger form, proving
its two uncovered statements unreachable rather than merely reporting them.

**And it is now known to be insufficient on its own.** Sub-project 15
established that **coverage.py emits no arc for a conditional expression**
(`tests/README.md` fact 87), so a region at 100% statements *and* 100% branches
can still hide an unexercised ternary arm behind every one. The scoped region contains
**six** conditional expressions, counted by reading: `saved_json = request_json
if store_ap_json else None` in `create_post`, `community.ap_id if
community.ap_id else community.name` once in each of the four notification
arms, and `author.ap_id if author.ap_id else author.user_name` in the
`NOTIF_USER` arm. **The plan enumerates them again against source** — the count
above is this spec's, taken at writing, and a spec's count is exactly the kind
of claim this campaign has learned to re-derive rather than inherit. Each arm
gets a test; the coverage figure will not find them.

## Out of scope

- **`Post.new`** (`app/models.py`), which is where `create_post`'s real work
  happens. It is a model method with many callers, not an ActivityPub entry
  point, and taking it would make this slice a model-layer slice.
- **`update_post_from_activity`'s type tails** — Video vote collections,
  Question poll rebuild, Event fields, the attachment walk, `POST_TYPE`
  detection with `make_image_sizes`, the moderator and admin notifications,
  cross-post recalculation. Roughly 107 uncovered, unmirrored, carrying D245.
  Still the campaign's largest single coherent unmirrored slice.
- The rest of `app/activitypub/util.py`: `make_image_sizes_async` (152
  uncovered), `process_report` (76), `new_instance_profile_task` (66),
  `undo_vote` (28).
- **The refresh trio's residual**, registered as D235.
- `notification_subscribers`, `blocked_users`, `blocked_communities`,
  `blocked_or_banned_instances`, `shorten_string` and `get_task_session` are
  driven or doubled here, not tested here.

## The defects this slice must confront

Same bounded fix authorisation sub-projects 5c through 15 carried: **defects
found inside these three functions are fixed test-first, each in its own
commit, separate from every test-only commit, and each proved by a mutation
that fails a named test.** Anything larger is registered.

The rule, as refined through three slices: **a defect is fixed when the correct
spelling already exists in the file and the change is mechanical; it is
registered when the fix would require choosing new behaviour for a case the
codebase has never handled.** Two corollaries, both earned:

- **Before ruling register-only for want of a correct spelling, grep the file
  for the twin** — a mirrored pair is not the only place a sibling can live.
- **The twin must also work.** Check the register for whether the sibling you
  are about to copy is itself a D-entry; copying a registered defect imports a
  broken guard and lets the slice claim a fix that fixes nothing.

### Authorised for fixing

**The `NOTIF_FEED` arm's `notifications_sent_to.add(notify_id)` is outside its
`if`.** Three sibling arms in the same function put it inside. The repair is
one indentation level, the correct spelling appears three times, and the effect
is a user silently skipped for every feed after the first that filtered them
out. This is exactly the shape the fix rule was written for.

**The plan must confirm the consequence before fixing it**, since the set is
also read by the earlier arms and the feed arm runs last: establish by test
whether the damage is confined to feed-to-feed suppression or reaches further.

### Registered by default; the plan may propose fixes

- **`NOTIF_USER` omits `blocked_users`.** A user who subscribed to an author
  and later blocked them still receives notifications.
- **`NOTIF_COMMUNITY` omits `blocked_communities`.** The same shape, one axis
  over.
- **The topic lookup is fetched conditionally but its subscribers are queried
  unconditionally.** `notification_subscribers(post.community.topic_id,
  NOTIF_TOPIC)` runs before `if post.community.topic_id:` guards the `Topic`
  fetch, and the loop body reads `topic.name`. **The plan establishes whether a
  community with no topic can produce a non-empty subscriber list**; if it can,
  this is a `NameError` on a peer-reachable path and moves to the authorised
  list.
- **The unread counter increments in all four arms**, where the reply-side twin
  recounts in one of its two branches. Sub-project 15 proved those are
  different operations and that a seed agreeing with the truth cannot tell them
  apart.
- **Each arm commits per recipient**, inside the loop, so a failure midway
  leaves some recipients notified and the rest not — and the `except Exception:
  session.rollback(); raise` cannot undo what was already committed. This is
  the partial-ingest shape sub-project 13's spec was built around, in a new
  place.

## Testing approach

**Entry: direct call.** `notify_about_post_task(post_id)` takes an id, and the
`app` fixture puts celery in eager mode, so calling the undecorated function is
the path a worker runs. `create_post(store_ap_json, community, request_json,
user, announce_id=None)` takes rows and a document.

**`get_task_session()` is a separate `Session`** with autoflush at SQLAlchemy's
default `True`, unlike `db.session`, which the app factory configures
`autoflush=False`. A commit inside the task does **not** expire the test's
objects, so **`db.session.refresh(obj)` is required before asserting** — the
harness sub-project 13 mapped, and the opposite of what sub-projects 14 and 15
needed.

**Subscriptions are seeded through `make_notification_subscription`**, which
sub-project 15 used. Read it before seeding; the four arms key on different
entity ids — a user id, a community id, a topic id and a feed id.

**Every test asserts on persisted row state** — a `Notification` row, its
fields, or a count — never merely on the absence of an exception.

**No vacuous assertions.** An unread-counter test seeds a non-default value and
asserts the increment; a "nobody notified" test seeds a non-zero `Notification`
count elsewhere so "none created" is distinguishable from "none exist".

**Ternaries are enumerated by reading, not measured.** Fact 87 is standing
doctrine: a region at 100% statements and 100% branches can still hide an
unexercised arm. Every `x if y else z` in the region gets both arms tested or a
stated reason.

**Mutation discipline.** Every guard mutation-tested with each conjunct dropped
separately, each killed by a distinct named test. The traps this campaign has
paid for, all of which apply here:

- **A test that reaches a guard's False side naturally cannot kill a mutant
  that forces that guard False.** Four arms of near-identical `if` chains make
  this the dominant hazard.
- **A negative test can fail to kill its guard because a later step
  independently produces the same outcome** — and with four sequential arms
  plus a shared de-duplication set, every "no notification" assertion has
  several routes.
- **An assertion on a value a later step would produce anyway cannot kill the
  arm that produced it early.**
- **Adding or removing a guard can unkill an existing test.** Re-run a guard's
  existing mutations after changing it.
- **Mutating a whole guard to `if True:` is a site-level proof, not a
  conjunct-level one.**
- **Five causes of a genuinely unkillable clause** are catalogued in
  `tests/README.md` fact 75. Name the cause; never invent a test to fake a
  kill.

**Docstrings must be true.** A docstring explaining why a fixture is safe is
itself a claim about production; sub-project 15 shipped one that inverted the
failure direction because it was written from the shape of the code rather than
from tracing what a miss would do. **Do not state a position, ordering,
distance or count in prose unless you have just read it** — prefer quoting
production code to describing it, since a quoted block is the one kind of claim
that can be audited mechanically.

**One pytest session at a time.** Any run over 600s is erroneous. The suite
produces **shifting false failures on a stale stack** — `IntegrityError:
duplicate key ... "ix_instance_domain"` during seeding, a different test each
run — so any surprising failure gets `./run_tests.sh --down` and a re-run
before it is believed.

## New test file

One new file, `tests/test_ap_notify_post.py`. No existing test file references
these three functions.

## The register's family index

The unguarded-peer-input family now spans **thirteen entries across two
sections** — D236, D238, D245, D246, D247, D255-D259, D264, D270-D272 — and
this campaign has twice registered a defect whose sibling was already
registered elsewhere.

**This slice adds a family index**: one section listing every member with its
site, its status, and whether a sibling copy is known elsewhere. It is a
navigation aid, not a rewrite — **no existing entry is renumbered, moved or
edited**, and the file's marked-correction convention is untouched.

## Global constraints

- Defects found in these three functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered.
- Findings are numbered from **D274**, and **all** live "Next free number"
  notes are updated in the same change. The historical notes are frozen
  records — leave them alone.
- The coverage floor for `app/activitypub/util.py` rises to the measured
  blended figure rounded down. It currently reads 71.
- Locate every code target by content, not by the line numbers in this spec.
- **Verify a citation's function attribution separately from its line range**,
  and derive a function's extent from an **unfiltered** `^def ` scan. This
  campaign has shipped false attributions whose line numbers were correct four
  times — one written as a correction, one an argument read off an adjacent
  call. **Correcting a claim does not correct its copies:** grep for the wrong
  claim, and for the entities a correction names.
- The full suite must pass. Only the controller runs it, one session at a time,
  and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## Success criteria

1. Full statement coverage of all three functions, **measured per-region from
   the coverage JSON and reported**, with any unreachable statement proved
   rather than asserted.
2. No guard survives any one conjunct being dropped, each kill by a distinct
   named test, with any unkillable clause named and its cause given.
3. **Every ternary in the region has both arms tested or a stated reason** —
   the criterion coverage cannot measure.
4. Every test asserts on persisted row state, not merely on the absence of an
   exception.
5. The `NOTIF_FEED` arm's misplaced `notifications_sent_to.add` is pinned, its
   consequence established by test, then fixed with a witnessed pre-fix failure
   and a mutation proof.
6. Every asymmetry in the tables above is either fixed with a mutation-proved
   test or registered with a stated reason, including the conditional topic
   lookup's reachability verdict.
7. `coverage_floors.ini` raised for `app/activitypub/util.py`.
8. The findings register carries every defect found, from D274, and the
   unguarded-peer-input family index is added without altering any existing
   entry.
9. Full suite green.

---

> **ANNOTATION, 2026-09-04, appended after this document's last original line; nothing above is revised.** The `NOTIF_FEED` defect this spec describes as present at `:165` and `:312` was **fixed** on 2026-09-04 by commit `0489dc1d`, and the "pinned … then fixed with a witnessed pre-fix failure" gate at `:312` was **not** what proved it: the defect turned out to be latent rather than live, so no pin could distinguish the two spellings and an equivalence argument plus a 70-mutation re-run against both code versions took that gate's place (Ruling J). Read **D275** in `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` for what production does now; this document is left unrevised on purpose, because it is the record of what was specified and its being wrong about the defect's liveness is the evidence for that ruling.

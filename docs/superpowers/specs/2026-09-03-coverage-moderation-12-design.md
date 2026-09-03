# Sub-project 12: the moderation and ban-removal cluster

**Date:** 2026-09-03
**Module:** `app/activitypub/util.py` — `delete_post_or_comment`,
`restore_post_or_comment`, `site_ban_remove_data`, `community_ban_remove_data`,
`ban_user`, `unban_user`
**Predecessors:** 5a-7 covered the inbox dispatcher; 8 webfinger; 9 the actor
profiles; 10 the collections; 11 the content objects. Those five slices took
`app/activitypub/routes.py` from 35% to **90.8%**. This is the first slice of
the module that routes.py delegates to.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D200**

## The unit

Six functions, in three mirrored pairs.

| Function | Lines at writing | Uncovered |
|---|---|---|
| `delete_post_or_comment` | 2204-2267 | **48** |
| `ban_user` | 2412-2509 | **44** |
| `unban_user` | 2510-2571 | **39** |
| `site_ban_remove_data` | 2309-2348 | **31** |
| `restore_post_or_comment` | 2268-2308 | **27** |
| `community_ban_remove_data` | 2349-2380 | **25** |

**214 uncovered statements** out of `app/activitypub/util.py`'s 1415.

They are taken together because they are three pairs of mirrored
implementations — do a thing, and undo it — and because, as in sub-projects 9,
10 and 11, the defects live in what the halves do **differently**. A pair whose
halves disagree does not merely behave oddly: it leaves persistent state that
neither half can repair, because the undo does not undo what the do did.

**These are the functions that destroy user content.** A wrong filter here does
not serve the wrong document; it soft-deletes the wrong rows, decrements the
wrong counters, or deletes files from disk and purges them from a CDN. That is
why this cluster is the module's first slice rather than its largest one.

### Citations drift

Every sub-project since 5c found its brief's line numbers stale, and this
slice's own fixes will move things. **Locate every target by code content.**
Quoted lines are navigational aids, never identifiers.

## These functions are already partially covered, and not by this campaign

`delete_post_or_comment` measures 48 uncovered rather than near-total because
four existing suites already drive these six through the inbox dispatcher:

- `tests/test_inbox_dispatch_lock_delete.py`
- `tests/test_inbox_dispatch_undo_moderation.py`
- `tests/test_inbox_dispatch_block.py`
- `tests/test_activitypub_ban_expiry.py` (which covers `parse_ban_expiry`, the
  helper both ban paths call — already documented with a full docstring)

**The plan reads all four before writing anything**, so new tests complement
rather than duplicate them, and so any fix this slice lands is checked against
the tests that already depend on these functions. Sub-project 9 hit exactly
this with `user_profile` and the spec that ignored it wasted a task.

## The asymmetries — this slice's whole point

### Pair 1: `delete_post_or_comment` / `restore_post_or_comment`

| Axis | delete | restore |
|---|---|---|
| Authorisation guard | four-way | **identical four-way** |
| Redis locks | every counter mutation | **none** |
| `community.post_reply_count` | `-= 1` | **never restored** |
| `post.reply_count_cross_posted` | `-= 1` when set | **never restored** |
| Cross-post guard | `url and cross_posts is not None` | **`url` only** |
| Notifications | deletes them, keeping report notifs | not recreated |
| `log_incoming_ap` type | `APLOG_DELETE` | `APLOG_UNDO_DELETE` |

The two counter rows are the finding: **a delete followed by a restore leaves
`community.post_reply_count` and `post.reply_count_cross_posted` permanently
one lower than they were.** Every subsequent cycle loses one more. Both columns
exist (`app/models.py`), and the delete side maintains both.

### Pair 2: `site_ban_remove_data` / `community_ban_remove_data`

| Axis | site | community |
|---|---|---|
| Scope filter | `user_id` only | `user_id` **and** `community_id` |
| User reply counter | **`blocked.reply_count = 0`** | `blocked.post_reply_count -= 1` |
| User post counter | `blocked.post_count = 0` | `blocked.post_count -= 1` |
| Community counters | `-= 1` per row | `-= 1` per row |
| Files | `delete_from_disk(purge_cdn=True)` | `delete_from_disk()` — **same effect** |
| Avatar / cover | deleted too | untouched |
| Query style | `db.session.query(...)` | legacy `.query` |

**`User` has no `reply_count` column.** It has `post_count` and
`post_reply_count`; `reply_count` belongs to `Post`. Assigning an undeclared
attribute to a SQLAlchemy instance sets a plain Python attribute that is never
persisted, so **site-banning a user silently never zeroes their reply count**,
while the community path decrements the real column correctly.

The avatar/cover asymmetry is defensible — a community ban should not destroy a
user's avatar — and the source says so.

**The `purge_cdn` difference is not a difference.** `File.delete_from_disk`'s
signature is `def delete_from_disk(self, purge_cdn=True)` (`app/models.py`), so
the community path's bare call passes exactly what the site path passes
explicitly. The two behave identically. This is worth registering precisely
because reading the two call sites side by side invites the opposite
conclusion: one names the flag and the other does not, which reads as a
deliberate distinction and is not one.

### Pair 3: `ban_user` / `unban_user`

| Axis | ban (instance) | ban (community) | unban (instance) | unban (community) |
|---|---|---|---|---|
| Reason path | `core_activity['summary']` | same | `['object']['summary']` | same |
| Existing-row guard | wraps **only** the row creation | wraps the **entire body** | n/a | n/a |
| Modlog | yes | yes | **none** | yes |
| Notify condition | unconditional | `if community.has_poster` | unconditional | `if community.has_poster` |
| Memoized caches cleared | 6 | 4 | 6 | 4 |

Two findings. **`unban_user`'s instance branch writes no modlog entry**, while
every other branch of both functions does — an instance-wide ban is recorded
and its reversal is not. And **the existing-row guard has different scope in
`ban_user`'s two branches**: re-banning instance-wide re-notifies and re-logs,
while re-banning in a community is a silent no-op.

The reason-path difference is consistent with an `Undo` wrapping the original
activity and is probably correct; it is recorded so a future reader need not
re-derive it.

## Goal

Full statement coverage of all six, and branch coverage sufficient that no
guard survives having any one of its conjuncts dropped. Expected effect:
`app/activitypub/util.py` moves from its measured **48.9424%** blended toward
**56%**, and the floor rises from 45 to the measured figure rounded down.

## Out of scope

The rest of `app/activitypub/util.py`, each a candidate for a later slice:
`make_image_sizes_async` (152 uncovered, image processing and S3),
`update_post_from_activity` (151) with `update_post_reply_from_activity` (64),
the refresh-profile trio (`refresh_feed_profile_task` 116,
`refresh_community_profile_task` 106, `refresh_user_profile_task` 88),
`create_post_reply` (83), `process_report` (76),
`new_instance_profile_task` (66), the notify pair (`notify_about_post_task` 48,
`notify_about_post_reply` 26), and the JSON builders routes.py doubled
(`post_to_page` 16, `comment_model_to_json` 10).

`add_to_modlog` (`app/utils.py`) and `Post.calculate_cross_posts`
(`app/models.py`) are driven with real rows here, not tested here.

## The defects this slice must confront

This slice carries the same bounded fix authorisation 5c through 11 had:
**defects found inside these six functions are fixed test-first, each in its
own commit, separate from every test-only commit, and each proved by a mutation
that fails a named test.** Anything larger is registered.

### Authorised for fixing

**`site_ban_remove_data` writes a column that does not exist.**
`blocked.reply_count = 0` sets a plain Python attribute on a `User` instance;
`User` declares `post_count` and `post_reply_count` and no `reply_count`. The
statement has no effect on the database and never raises. The correct target is
`post_reply_count`, which is what `community_ban_remove_data` decrements.

This is unambiguous, self-contained, provable by mutation, and inside the six.
**Fixing it changes a stored number**: a site-banned user's reply count will
start reading 0 where it previously kept its pre-ban value. That is the
intended change.

### Registered by default; the plan may propose fixes

The remaining asymmetries are registered unless the plan argues a specific one
is small, self-contained, provable by mutation, and inside these six functions.

- **`restore_post_or_comment` never restores `community.post_reply_count` or
  `post.reply_count_cross_posted`.** The strongest candidate for a fix after
  the authorised one, and the plan should argue it explicitly. Registered by
  default because correcting a counter changes numbers users already see, and
  because the drift is historical: existing rows are already wrong by an
  unknown amount that this fix would not repair.
- **`restore_post_or_comment` takes no redis locks** where `delete_post_or_comment`
  wraps every counter mutation in one. Same counters, same concurrent exposure,
  opposite treatment.
- **`restore_post_or_comment`'s cross-post guard drops a conjunct** —
  `if to_restore.url:` against delete's `if to_delete.url and
  to_delete.cross_posts is not None:`.
- **`unban_user`'s instance branch writes no modlog entry.**
- **`ban_user`'s existing-row guard has different scope in its two branches.**
- **The `purge_cdn` call-site difference is cosmetic, not behavioural** — the
  parameter defaults to `True`, so both paths purge. Registered because the
  asymmetric spelling invites a reader to infer a distinction that does not
  exist.
- **The two ban-removal functions use different query styles** —
  `db.session.query(...)` against the legacy `.query`, whose `Query.all()`
  auto-deduplication masked a malformed join in sub-project 10 (D171).

## Testing approach

**Entry: direct function calls with real rows.** Not through the inbox
dispatcher — the four existing suites already do that, and repeating it would
measure the dispatcher rather than these six. Each test builds its actors and
content with the factories, calls the function, and asserts on the resulting
database state.

**`redis_double` is required for `delete_post_or_comment`.** It takes
`redis_client.lock(...)` on four separate keys. The fixture
(`tests/conftest.py`) patches `app.redis_client`, and the function does
`from app import redis_client` **inside its body** — a re-executed import, so
the single attribute patch reaches it. Without the fixture the test talks to
the real, shared, never-truncated compose Redis; the fixture's own docstring
records a test that went green doing exactly that.

**`File.delete_from_disk` must be doubled.** It touches the filesystem and
purges a CDN (`app/models.py`). Both ban-removal functions call it in a loop.
Double it at its binding site and assert on the calls — `purge_cdn`'s value is
itself one of the registered asymmetries and must be observable.

**`add_to_modlog` and `Post.calculate_cross_posts` run for real.** Both are
ordinary database work, and the modlog rows are the observable that proves
`unban_user`'s missing entry.

**Seeding.** `make_user`, `make_community`, `make_post`, `make_post_reply`,
`make_community_member`, `make_instance`, `seed_community_owner` and
`make_site` all exist. `InstanceBan` has `make_instance_ban`. `CommunityBan`,
`CommunityJoinRequest`, `NotificationSubscription` and `ModLog` rows need
checking against the models; **the plan confirms what each needs rather than
assuming**, and adds factories only where a raw row would be repeated across
tasks. Sub-project 8's spec claimed no factory was needed and was wrong.

**Counter assertions need a seeded baseline.** Every counter these functions
touch defaults to 0, so a test asserting a decrement must set a non-zero
starting value explicitly or it cannot tell a working decrement from an absent
one. This is the no-vacuous-assertion rule in its counting form.

**The delete/restore counter findings need a round trip.** Asserting that
`restore` fails to increment a counter requires seeding the counter, deleting,
asserting the decrement, restoring, and asserting the counter did **not** come
back. A test that only calls `restore` proves nothing, because the counter was
never decremented in the first place.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped separately, each killed by a distinct named test. The four-way
authorisation guard in `delete_post_or_comment` and `restore_post_or_comment`
is the largest in the campaign so far — four disjuncts, each needing its own
fixture and its own kill. Carry forward the patterns already recorded:

- **A clause whose value equals what the factory always produces cannot be
  killed** by any test using that factory unmodified.
- **An assertion comparing a response value to the object's own column is
  vacuous whenever the factory never sets that column** (harness fact 50).
- **A clause duplicated across two call sites has two ways to be wrong.**
  Mutate one site at a time — both ban-removal functions duplicate the
  `deleted=False` filter and the counter decrements.
- **A guard behind an earlier `abort` or `return` is never reached.**

**Docstrings must be true**, including after the fix. After inverting any pin,
check which branch it used to cover — and check the whole file for counts and
cross-references, which sub-projects 10 and 11 each lost review rounds to.

**One pytest session at a time**, and stopping `run_tests.sh` on the host does
not kill pytest in the container. Any run over 600s is erroneous: the podman
stack degrades, and `./run_tests.sh --down` before a coverage run restores it
(sub-project 11 hit the 600s wall and had to re-measure).

## New test file

One new file, `tests/test_ap_moderation.py`. Six functions across 214
statements is one coherent unit sharing a harness, a seeding surface and the
pair comparisons this slice exists to make. Splitting deletion from banning
would hide the shared authorisation shape and the shared counter maintenance.

## Global constraints

- Defects found in these six functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered.
- Findings are numbered from **D200**, and **both** live "Next free number"
  notes are updated in the same change. The historical notes are frozen
  records — leave them alone.
- The coverage floor for `app/activitypub/util.py` rises to the measured
  blended figure rounded down. It currently reads 45.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a time,
  and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## Success criteria

1. All six functions reach full statement coverage.
2. No guard survives any one conjunct being dropped, each kill by a distinct
   named test — including all four disjuncts of the shared authorisation guard.
3. Every test asserts on persisted database state, not only on a return value.
4. `site_ban_remove_data`'s write to a non-existent column is pinned, then
   fixed, with a witnessed pre-fix failure and a mutation proof.
5. The delete/restore counter drift is demonstrated by a round-trip test, then
   fixed or registered with a stated reason.
6. Every asymmetry in the three tables above is either fixed with a
   mutation-proved test or registered with a stated reason.
7. `coverage_floors.ini` raised for `app/activitypub/util.py`.
8. The findings register carries every defect found, from D200.
9. Full suite green.

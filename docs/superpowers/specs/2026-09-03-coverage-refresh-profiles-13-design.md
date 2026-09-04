# Sub-project 13: the refresh-profile trio

**Date:** 2026-09-03
**Module:** `app/activitypub/util.py` — `refresh_user_profile_task`,
`refresh_community_profile_task`, `refresh_feed_profile_task`
**Predecessors:** 5a-7 covered the inbox dispatcher; 8 webfinger; 9 the actor
profiles; 10 the collections; 11 the content objects; 12 the moderation and
ban-removal cluster. 8-11 took `app/activitypub/routes.py` from 35% to 90.8%;
12 was the first slice of `util.py` and took it from 48.9% to 55.1%.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D213**

## The unit

Three functions, in one mirrored trio.

| Function | Lines at writing | Uncovered |
|---|---|---|
| `refresh_feed_profile_task` | 1016-1163 | **116** |
| `refresh_community_profile_task` | 784-1007 | **106** |
| `refresh_user_profile_task` | 655-775 | **88** |

**310 uncovered statements** out of `app/activitypub/util.py`'s 1215.

They are taken together because they are three near-identical answers to one
question — *fetch a remote actor's document and update our copy of it* — and
because, as in sub-projects 9 through 12, the defects live in what they do
**differently**. This is the campaign's first trio rather than a pair, so an
asymmetry can now be a majority-of-three rather than a straight disagreement,
which is a stronger signal about which half is wrong.

These are the functions that keep this instance's picture of the fediverse
current. They run as Celery tasks against documents a remote peer controls, so
every branch here is reachable by a peer choosing what to send.

### Citations drift

Every sub-project since 5c found its brief's line numbers stale, and this
slice's own fixes will move things. **Locate every target by code content.**
Quoted lines are navigational aids, never identifiers.

## One of the three is already partially covered

`tests/test_ap_refresh_community_profile.py` exists (413 lines) and drives
`refresh_community_profile_task`. **It covers only the legacy
`lemmy:tagsForPosts` flair loop and the commit that precedes it**, and says so
in its own header: the actor re-fetch and the moderators, followers and
featured collections are explicitly "out of scope and reported, not tested."

**The plan reads that file before writing anything.** It carries harness
knowledge this slice depends on, and two reusable helpers
(`_community_awaiting_refresh`, `_actor_document`). Sub-project 9 wasted a task
by ignoring an existing partial suite, and sub-project 12's spec avoided that
by naming its four.

## The asymmetries — this slice's whole point

| Axis | user | community | feed |
|---|---|---|---|
| Guard | `user and user.instance_id and user.instance.online()` | `community and community.instance.online()` | `feed and feed.instance.online() and not feed.is_local()` |
| `instance_id` checked | **yes** | **no** | **no** |
| `is_local()` checked | no | no | **yes** |
| Takes `activity_json` | no | **yes** | no |
| Always fetches | yes | only when `activity_json` falsy | yes |
| Retry inner catch | **bare `except:`** | `except Exception:` | `except Exception:` |
| Third fallback | **`signed_get_request`** | none | none |
| `.json()` decode | `try/except JSONDecodeError`, bumps `instance.failures` | **unguarded** | **unguarded** |

All three share the same outer shape: `get_task_session()`, `patch_db_session`,
a `get_request` with an `Accept: application/activity+json` header, a
`time.sleep(randint(3, 10))` before one retry, then a `status_code == 200`
gate before applying the document.

## Goal

Full statement coverage of all three, and branch coverage sufficient that no
guard survives having any one of its conjuncts dropped. Expected effect:
`app/activitypub/util.py` moves from its measured **55.1080%** blended toward
**64%**, and the floor rises from 55 to the measured figure rounded down.

## Out of scope

The rest of `app/activitypub/util.py`, each a candidate for a later slice:
`make_image_sizes_async` (152 uncovered, image processing and S3),
`update_post_from_activity` (151) with `update_post_reply_from_activity` (64),
`create_post_reply` (83), `process_report` (76), `new_instance_profile_task`
(66), the notify pair (`notify_about_post_task` 48, `notify_about_post_reply`
26), and the JSON builders routes.py doubles (`post_to_page` 16,
`comment_model_to_json` 10).

The legacy `lemmy:tagsForPosts` flair loop is covered by
`tests/test_ap_refresh_community_profile.py` and is not re-tested here.
`find_flair_or_create`, `get_request`, `signed_get_request` and
`Instance.online()` are driven or doubled here, not tested here.

## The defects this slice must confront

This slice carries the same bounded fix authorisation 5c through 12 had:
**defects found inside these three functions are fixed test-first, each in its
own commit, separate from every test-only commit, and each proved by a mutation
that fails a named test.** Anything larger is registered.

### Authorised for fixing

**The community and feed tasks crash on an actor whose instance is unknown.**
`refresh_user_profile_task` guards `user.instance_id` before dereferencing
`user.instance`; the other two go straight to `community.instance.online()` and
`feed.instance.online()`. `Instance` is a nullable relationship, so a row with
`instance_id` NULL raises `AttributeError: 'NoneType' object has no attribute
'online'`. Two of three get this wrong and one gets it right, which is what
makes it an oversight rather than a choice.

**The community and feed tasks crash on malformed JSON from a peer.**
`refresh_user_profile_task` wraps `actor_data.json()` in
`try/except JSONDecodeError`, increments `user.instance.failures` and returns.
The other two call `actor_data.json()` unguarded, so a peer returning HTTP 200
with a non-JSON body raises out of the task. Again two of three wrong, one
right — and this one is remotely triggerable by any peer this instance
refreshes from.

Both fixes take the shape the user task already uses. **Match it rather than
inventing a fourth spelling**, and note that the user task's handler also
increments `failures`, which is behaviour the other two would gain.

### Registered by default; the plan may propose fixes

- **`refresh_user_profile_task` uses a bare `except:`** where its siblings use
  `except Exception:`. A bare `except` catches `KeyboardInterrupt` and
  `SystemExit`, so a worker shutdown mid-refresh is swallowed into the
  signed-GET fallback path. Registered rather than fixed because changing it
  changes which failures retry and which propagate.
- **Only the user task has a `signed_get_request` third fallback.** A peer that
  requires HTTP signatures to serve its actor document is refreshable for users
  and not for communities or feeds.
- **Only the feed task checks `is_local()`.** The other two will happily
  re-fetch a local actor from its own public URL if one somehow has an
  `instance_id`.
- **Only the community task takes `activity_json`**, so only it can be driven
  from a document a caller already holds. The other two always fetch, which is
  why they are harder to test and why that difference is worth recording.
- **All three sleep `randint(3, 10)` seconds inline before retrying.** In a
  Celery task that is a worker blocked on a peer's failure, not a backoff the
  broker manages.

## Testing approach

**Entry: direct function call.** These are Celery tasks, but every caller
invokes them inline under `current_app.debug` and the `app` fixture puts celery
in eager mode, so calling the undecorated function is the same code path a
worker runs, with no mock anywhere. `tests/test_ap_refresh_community_profile.py`
established this and the plan should not re-derive it.

**`http_mock` and `federation_peer` are required** for the user and feed tasks,
which have no `activity_json` parameter and therefore always fetch.
`block_outbound_http` (`tests/conftest.py`) raises on any unmocked request, so
a test that provokes an unexpected fetch fails loudly rather than passing
quietly — that is a feature and the plan should rely on it.

**`no_real_sleeping` is required for every retry test.** All three call
`time.sleep(randint(3, 10))` between the first failure and the retry. Without
the fixture a retry test sleeps for up to ten real seconds.

**The three collections are opt-in.** Moderators, followers and featured are
each fetched only when the corresponding URL is set on the row, so clearing
them leaves the task with nothing to fetch beyond the actor document itself.
`make_community` supplies `ap_followers_url`; the existing suite clears it.

**`get_task_session()` is a plain `Session` with autoflush at SQLAlchemy's
default `True`** — unlike `db.session`, which the app factory configures with
`session_options={"autoflush": False}`. The plan must know which of its
assertions depend on that and say so; the existing suite documents one case
where it is load-bearing and one where it is not.

**A crash here is a partially-applied ingest, not a clean abort.** Each task
commits the refreshed profile and only then walks the remaining structures, and
`except Exception: session.rollback(); raise` cannot undo a commit. So a test
asserting only "nothing raised" cannot distinguish a real fix from a guard that
abandoned the whole document. **Every test asserts on the row's own columns**,
not merely on the absence of an exception.

**Seeding.** `make_user`, `make_community`, `make_local_feed`, `make_instance`
and `seed_community_owner` all exist. A row with `instance_id` NULL is what the
first authorised fix needs, and no factory produces one — **the plan confirms
that against the factories rather than assuming it**, and sets the column
explicitly if so.

**No vacuous assertions.** `Instance.dormant` and `Instance.gone_forever` both
default to `False`, so `online()` is true for a factory-built instance; a test
that depends on an offline instance sets one of them explicitly.
`Instance.failures` defaults to 0, so the JSONDecodeError test must seed a
non-zero baseline or it cannot tell an increment from an absent one.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped separately, each killed by a distinct named test. Carry forward the
patterns this campaign has recorded, especially the one it has now hit three
times: **a filter or guard whose excluded set is empty under the fixture is
unkillable** — the remedy is a fixture that creates the row the guard exists to
exclude, not a weaker assertion.

**Docstrings must be true**, including after the fixes, and including counts.
Sub-projects 10, 11 and 12 each lost review rounds to a claim corrected in one
place and left standing in another. When a count or a claim changes, grep the
**old** value.

**One pytest session at a time.** Any run over 600s is erroneous; the podman
stack degrades and `./run_tests.sh --down` restores it.

## New test file

One new file, `tests/test_ap_refresh_profiles.py`. Three tasks across 310
statements is one coherent unit sharing a harness, a seeding surface and the
trio comparisons this slice exists to make. The existing
`tests/test_ap_refresh_community_profile.py` stays as it is — it covers a
different part of one of the three, and merging them would bury both.

## Global constraints

- Defects found in these three functions are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside them is registered.
- Findings are numbered from **D213**, and **both** live "Next free number"
  notes are updated in the same change. The historical notes are frozen
  records — leave them alone.
- The coverage floor for `app/activitypub/util.py` rises to the measured
  blended figure rounded down. It currently reads 55.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a time,
  and the controller supplies every coverage figure.
- **Delete nothing** the task did not create. `claude_test` in the repository
  root is not the campaign's.

## Success criteria

1. All three functions reach full statement coverage.
2. No guard survives any one conjunct being dropped, each kill by a distinct
   named test.
3. Every test asserts on persisted row state, not merely on the absence of an
   exception.
4. Both crash paths — the missing `instance_id` guard and the unguarded
   `.json()` — are pinned, then fixed, each with a witnessed pre-fix failure
   and a mutation proof.
5. Every asymmetry in the table above is either fixed with a mutation-proved
   test or registered with a stated reason.
6. `coverage_floors.ini` raised for `app/activitypub/util.py`.
7. The findings register carries every defect found, from D213.
8. Full suite green.

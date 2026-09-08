# Sub-project 28: closing `app/shared/tasks/follows.py`, and two residuals

**Date:** 2026-09-08
**Branch:** `blentz`
**Predecessor:** sub-project 27, commits `d16d89dc..7a4e9ac9`, kept as-is

## Goal

Take `app/shared/tasks/follows.py` from 16.84% to 100% statement and branch
coverage, land three production changes, and close the two near-miss residuals
in `notes.py` and `pages.py` so every floored module in
`app/shared/tasks/` sits at 100 rather than two lingering one- and two-point
gaps.

Measured at `7a4e9ac9`: `follows.py` 152 statements, 120 missing, 44 branches,
1 partial. `notes.py` 99.09% (`:100-101` missing). `pages.py` 98.65%
(`:107-108` missing, plus three untaken branch arms).

After this round `app/shared/tasks/` is 13 of 14 modules at 100%, leaving only
`maintenance.py` at 636 statements — which needs its own decomposition spec.

## What makes this module different from the last four

Every module this campaign has closed since sub-project 24 was a **fan-out**:
`following_instances()`, a recipient guard, an Announce loop. `follows.py` has
none. Its five tasks each send **at most one request**, directly, to a single
actor's inbox. There is no recipient loop, no `domains_sent_to`, no Announce.

What it has instead is **state**: `CommunityJoinRequest`, `FeedJoinRequest` and
`UserFollowRequest` rows created, read for their `uuid`, and deleted. The
defects below are all in the ordering of those operations against
`session.commit()`, which is a class this campaign has not yet had to test.

## Module structure

277 lines, five `@celery.task` functions:

| Function | Lines | Sends |
|----------|-------|-------|
| `join_community` | `:38-108` | one Follow to `community.ap_inbox_url` |
| `leave_community` | `:111-157` | one Undo to `community.ap_inbox_url` |
| `leave_feed` | `:160-210` | one Undo to `feed.ap_inbox_url` |
| `follow_user` | `:213-238` | one Follow to `to_follow.ap_inbox_url` |
| `unfollow_user` | `:241-277` | one Undo to `to_follow.ap_inbox_url` |

All five are wired through `task_selector` (`app/shared/tasks/__init__.py:20-22`,
`:58-59`) and all five have live callers: `app/shared/community.py:38` and
`:61`, `app/shared/feed.py:125`, `app/shared/user.py:275` and `:295`.

`join_community` is the widest, with a three-way `src` fork
(`SRC_WEB`/`SRC_PLD`/`SRC_API`) inside each of two early-return guards, and a
different return value per arm.

## THE CENTRAL DEFECT, AND WHY THE ROUND MUST OBSERVE IT BEFORE FIXING IT

`leave_community` reads a deleted row's attribute:

```python
125:             join_request = session.query(CommunityJoinRequest).filter_by(...).first()
126:             session.delete(join_request)
127:             session.commit()
...
134:             follow_id = f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"
```

`app/__init__.py:81` constructs `SQLAlchemy` without overriding
`expire_on_commit`, so the default `True` applies (harness fact 58). After
`:127`, `join_request` is a deleted instance with expired attributes;
`:134`'s read of `.uuid` should raise `ObjectDeletedError` before the Undo is
ever built.

`unfollow_user` does the same thing at `:251-253`: delete at `:251`, commit at
`:252`, then read `.uuid` at `:253`.

**`leave_feed` does it correctly, forty lines below the first defect:**

```python
175:             join_request = session.query(FeedJoinRequest).filter_by(...).first()
176:             if join_request:
177:                 uuid = join_request.uuid
178:             session.query(FeedJoinRequest).filter_by(...).delete()
179:             session.commit()
```

The correct idiom is in the same file, in the function immediately after. This
is D332's shape — knowledge not applied rather than knowledge missing — and
the register entry should say so.

**This is a prediction, not a measurement.** The first production task must
OBSERVE the failure before changing anything: write the test, run it, and paste
what actually happens. If `ObjectDeletedError` is not what comes out — if
SQLAlchemy returns a stale value, or the attribute survives, or the path is
unreachable for some reason this spec has missed — then the fix changes shape
and the register entry must describe what was found rather than what was
expected. A round that fixes a defect it never watched fail has proved nothing.

## Production changes

Three, matching sub-projects 24 through 27.

### PC1 — `leave_community`, capture the uuid before the delete

Read `join_request.uuid` into a local before `:126`'s `session.delete`, and
build `follow_id` from the local. Match `leave_feed:175-177`'s shape.

**Scope note, deliberately narrow:** `:125`'s `.first()` can return `None`, and
`session.delete(None)` raises `UnmappedInstanceError`. PC1 does **not** change
that. Adding a `None` guard would alter what happens when a user leaves a
community they never had a join request for, which is a behaviour question this
round can register better than it can answer. PC1 is strictly the ordering fix,
so the claim it makes is exactly one claim.

### PC2 — `unfollow_user`, the same fix at `:251-253`

Identical edit, identical reason. `unfollow_user` already has the `if
join_request:` guard `leave_community` lacks, so only the ordering is wrong
here.

### PC3 — `leave_feed`, restore the missing `raise`

`:207-208` is `except Exception:` / `session.rollback()` with no `raise`. Every
other task in this module and every task in this package re-raises. So a
failure inside `leave_feed` is swallowed: the caller sees success, Celery
records none, and the user's feed membership silently fails to leave.

Add `raise`, making it `except Exception: session.rollback(); raise`.

**This one changes what escapes to Celery**, for a task that has been
swallowing failures for its whole life. The test that pins it must assert the
exception propagates, and the register entry must record that the change is
deliberate and what it makes visible.

## Findings to register, not fix

- **`leave_community:125`'s unguarded `.first()`** — `session.delete(None)`
  raises. Out of PC1's scope by design; recorded with the reason.
- **`leave_feed:187`'s possibly-unbound `uuid`** — `:176`'s `if join_request:`
  guards the assignment but nothing guards the use at `:187`, so a leave with
  no `FeedJoinRequest` row raises `UnboundLocalError`. Whether that path is
  reachable is a question the coverage work will answer; if it proves
  unreachable, the finding records the proof instead.
- **`follow_user` and `unfollow_user` never call `patch_db_session`** — unlike
  `join_community`, `leave_community` and `leave_feed`. This is D314's shape.
  Record whether it matters: those two functions may not touch anything that
  reads `db.session`, in which case the omission is inert and the entry should
  say so rather than implying a latent bug.

## The two residuals

**`notes.py:98-101` and `pages.py:105-108` are the same defect twice:**

```python
try:
    recipient = search_for_user(user_name)
except:
    pass
```

A bare `except: pass` around a mention lookup, in both files. Two statements
each, uncovered because no test makes `search_for_user` raise. Closing them
means one test per file that forces the exception — and the tests should
assert what the surrounding code then does with an unset `recipient`, not
merely that the lines executed.

**`pages.py` also has three untaken branch arms:** the false arms of `:270`'s
and `:333`'s `if not community.local_only:`, and `:312`'s `if 'name' in page:`.
Each needs a test that takes the arm coverage has never taken.

Closing both files takes `notes.py` from floor 99 and `pages.py` from floor 98
to 100.

## Test architecture

Two new files plus edits to two existing ones:

- `tests/test_shared_tasks_follows.py` — new, the whole module.
- `tests/test_shared_tasks_send_reply.py` and
  `tests/test_shared_tasks_send_post.py` — existing; the residual tests belong
  with the functions they cover rather than in a new file. Confirm which file
  owns `notes.py`'s and `pages.py`'s mention parsing before adding to either.

**Oracles.** Unchanged from the last four rounds. A spurious send is invisible
to a delivered-inboxes set and to a route call count, because
`signature.py:143`'s `except Exception as e:` swallows respx's unmatched-request
assertion into an `ActivityPubLog` failure row (fact 148, D332). Where a test
needs "nothing was sent", the oracle is a row count. `post_request` writes its
row unconditionally at `signature.py:105`, before the transport.

**A new oracle this module needs.** Several of these tasks are defined by the
ROW they create or delete, not by what they send: `join_community` writes a
`CommunityJoinRequest`, `leave_community` deletes one, `follow_user` writes a
`UserFollowRequest`. A test asserting only on the wire misses half of what each
task does. Assert both.

**Fact 153 applies throughout.** These tasks write through
`get_task_session()`'s own `Session`, so a test asserting on an ORM object's
attributes after the task runs reads stale state unless it calls
`db.session.expire_all()` first. Row counts through a fresh query are immune;
attribute reads on an object the test built earlier are not. This module's
tests will do more attribute-reading than the last four, because the state IS
the behaviour.

**`join_community`'s `src` fork needs one test per arm.** `SRC_WEB` flashes and
returns `None`; `SRC_PLD` returns a dict; `SRC_API` raises. Under
`task_always_eager` the wrapper returns the value directly (fact 146), so the
return value is observable. `flash()` requires a request context — establish
early whether the existing harness provides one, because three of the arms
call it.

## Deliverables

- `tests/test_shared_tasks_follows.py`, new.
- `app/shared/tasks/follows.py` at 100% statement and branch coverage.
- `notes.py` and `pages.py` at 100%, their floors raised from 99 and 98.
- Three production changes: PC1 `leave_community`, PC2 `unfollow_user`,
  PC3 `leave_feed`.
- `coverage_floors.ini` gains `app/shared/tasks/follows.py = 100` — 19 entries.
- Mutations covering both uuid-ordering fixes, PC3's `raise`, `join_community`'s
  two guards and its three `src` arms, and each residual branch arm.
- Findings from D337; facts from 155.
- Full suite green, all 19 floors met against a report whose mtime postdates
  the run.

## Risks

**The central defect may not behave as predicted.** The whole round is shaped
around `ObjectDeletedError` at `leave_community:134`. If observation shows
something else, PC1 and PC2 change shape. The plan must put that observation in
its first production task, before any fix.

**PC3 changes what escapes.** Adding `raise` to a task that has never raised
could surface failures elsewhere in the suite. The full-suite run is where that
shows up, and it is a real result rather than a flake if it does.

**`flash()` and request context.** Three of `join_community`'s six early-return
arms call `flash()`, which needs a request context. The `app` fixture pushes
only an app context (this is why `patch_db_session` is live in these tests). If
`flash()` raises outside a request context, those arms need a different
approach and the plan should say which.

**The residuals are in files this round does not otherwise touch.** Adding
tests to `tests/test_shared_tasks_send_reply.py` and
`tests/test_shared_tasks_send_post.py` means re-deriving citations in files with
their own conventions and their own line numbering. Sub-project 27 spent a fix
round on eleven citations that went stale from a two-line deletion; the same
discipline applies here.

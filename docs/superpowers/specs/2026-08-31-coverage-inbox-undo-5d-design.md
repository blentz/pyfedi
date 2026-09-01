# Sub-project 5d: the inbox dispatcher's Undo arm

**Date:** 2026-08-31
**Module:** `app/activitypub/routes.py`, the `Undo` arm of `process_inbox_request`
**Predecessors:** 5a (preamble, Announce unwrap, vote arms, Flag/Move/QuoteRequest),
5b (Follow/Accept/Reject), 5c (Delete/Lock/Add/Remove/Block)
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D104**

## The unit

The `Undo` arm runs from `if core_activity['type'] == 'Undo':` to the
`'Unmatched activity'` fall-through immediately before
`if core_activity['type'] == 'QuoteRequest':`. At the time of writing that is
`routes.py:1676-1883`, 208 lines carrying **160 statements, of which exactly one
executes today** — the arm's own `if`, reached because 5a's preamble tests walk
past it. Statement coverage inside the arm is 0.6%.

The arm dispatches on `core_activity['object']['type']` through seven
sub-types, each terminating in its own `return`:

| Sub-type | What it does | Principal delegates |
|---|---|---|
| `Follow` | Unsubscribe from a community, a feed, or a user | `find_actor_or_create_cached`, `community_membership`, `feed_membership` |
| `Delete` | Restore a previously deleted post, comment or PM | `find_liked_object`, `restore_post_or_comment`, `announce_activity_to_followers` |
| `Like` / `Dislike` | Retract an upvote or downvote | `undo_vote`, `announce_activity_to_followers` |
| `Announce` | Retract a boost from a microblogging platform | `announce_target_uri`, `undo_boost` |
| `Lock` | Unlock a post or a comment | `add_to_modlog` |
| `Block` | Undo a site ban or a community ban | `unban_user` |
| `ChooseAnswer` | Clear a reply's `answer` flag under a Redis lock | none |

An eighth path exists and is not a sub-type: an `Undo` whose inner type matches
none of the seven falls through to
`log_incoming_ap(id, APLOG_MONITOR, APLOG_PROCESSING, request_json, 'Unmatched activity')`.
It is in scope.

### Citations are stale by construction

Every task in 5c reported the brief's line numbers stale, and 5c's own Task 4
shifted everything after `:1300` by de-indenting a block. This spec's line
numbers are correct as of writing and will not stay correct: the three Undo/Lock
fixes below change line counts inside the arm. **Locate every target by code
content, not by the line numbers quoted here.** Quoted lines are navigational
aids, never identifiers.

## Goal

Full statement coverage of all seven sub-types and the fall-through, and
sufficient branch coverage that no guard in the arm survives having either of
its conjuncts dropped. Expected effect on the module: `app/activitypub/routes.py`
moves from its current measured **50.610%** blended toward **58-60%**, and the
floor in `coverage_floors.ini` rises to the measured figure rounded down.

## Out of scope

`Create` and `Update` (`routes.py:1193` onward) keep their own future spec.
Everything 5a, 5b and 5c cover stays covered; this slice adds to the same module
and the same floor. The delegates themselves — `undo_vote`, `undo_boost`,
`restore_post_or_comment`, `unban_user` — are doubled here, not tested here.

## The four fixes

This slice departs from the campaign's register-don't-fix default for four
defects, on the project owner's explicit instruction, in the same shape 5c used:
each fix is written test-first and committed separately from every test-only
commit, and each is proved by mutation — reverting the fix must fail a named
test.

Three of the four are in Undo/Lock, which is the least correct code in the arm.

### Fix 1 — `post` dereferenced on the `post_reply` branch

In the `if post_reply:` branch, the modlog call reads

```python
add_to_modlog('unlock_post_reply', actor=mod, target_user=post.author, reason=reason,
              community=post.community, reply=post_reply, ...)
```

`post` is `None` whenever that branch is taken, because the branch is only
reachable when the object resolved to a `PostReply` rather than a `Post`. Every
federated comment *unlock* therefore raises `AttributeError` after the reply has
already been unlocked and committed.

**This is the exact twin of D97**, fixed in `b79f43f9` on 2026-08-31 in the
`Lock` arm's own comment branch, where the same three attributes were read off
the same always-`None` `post`. The lock and unlock paths were evidently written
from each other. Fix: `post_reply.author` and `post_reply.community`.

### Fix 2 — membership tested against a dict, not a string

```python
if '/post/' in core_activity['object']:
    post = Post.get_by_ap_id(target_ap_id)
elif '/comment/' in core_activity['object']:
    post_reply = PostReply.get_by_ap_id(target_ap_id)
```

`core_activity['object']` here is the inner `Lock` activity, a **dict** — the
arm reached this code by reading `core_activity['object']['type']`. `in` against
a dict tests its keys, so neither `'/post/'` nor `'/comment/'` can ever match,
both branches are dead, and every Undo/Lock falls to the `else`. The intended
operand is `target_ap_id`, the string assigned immediately above, which is what
the sibling `Lock` arm tests. Fix: test `target_ap_id`.

Note the interaction with Fix 1: today the dead branches mean `post_reply` is
only ever set by the `else` fallback, which reaches it after `Post.get_by_ap_id`
returns `None`. Fixing this makes the `/comment/` branch live and so makes
Fix 1's crash reachable by a second route. The two fixes must land together, and
the plan sequences them in one task for that reason.

### Fix 3 — the `else` binds to the wrong `if`

The structure is `if post: ... if post_reply: ... else: log FAILURE 'Unlock: post not found'`.
The `else` belongs to `if post_reply:`, not to the pair. A successful *post*
unlock therefore logs `APLOG_SUCCESS` and then, because `post_reply` is `None`,
immediately logs `APLOG_FAILURE 'Unlock: post not found'` for the same activity.
Every successful post unlock records a contradiction in `ActivityPubLog`.

Fix: make the failure log conditional on neither having been found, matching the
`Lock` arm's own structure.

### Fix 4 — a dead `isinstance` in ChooseAnswer

```python
if isinstance(core_activity['object'], str):
    target_ap_id = core_activity['object']
else:
    target_ap_id = core_activity['object']['object']
```

Control only arrives here after `core_activity['object']['type'] == 'ChooseAnswer'`
succeeded, which a `str` cannot satisfy — subscripting a string by `'type'`
raises `TypeError`. The `str` branch is unreachable. Same equivalent-mutant class
as D96 (Block's dead Mastodon-path `isinstance`) and D95 (Remove's dead
`if proceed:`). Fix: drop the dead branch and assign directly.

Fix 4 is the only one of the four that changes no behaviour. It is included
because leaving it makes the arm's coverage permanently unreachable at that
line, which would otherwise have to be explained in the register forever.

## Testing approach

Unchanged from 5a-5c, which is the point — these facts are established and are
restated here only so a task brief need not rediscover them.

**Entry.** `dispatch(activity, store_ap_json=True)` from
`tests/test_inbox_dispatch_preamble.py` calls `process_inbox_request` directly.
That is production's own `current_app.debug` branch, not a test-only shortcut.

**Doubling at the binding site.** `routes.py` imports every delegate by name, so
patching the defining module leaves routes' own reference pointing at the
original. `record_moderation(monkeypatch, *names)` from
`tests/test_inbox_dispatch_lock_delete.py` patches each name on
`app.activitypub.routes` and records calls. All of `undo_vote`, `undo_boost`,
`announce_target_uri`, `find_liked_object`, `restore_post_or_comment`,
`unban_user`, `add_to_modlog`, `community_membership` and `feed_membership` are
imported into routes; `announce_activity_to_followers` is defined there. Both
shapes patch the same way.

**The fakeredis lock limitation.** Undo/ChooseAnswer runs its write inside
`with redis_client.lock(f"lock:post_reply:{post_reply.id}", timeout=10, blocking_timeout=6):`.
`tests/conftest.py:429-444` documents that this fixture's `fakeredis` instance
cannot serve a redis-py lock: `__enter__`/`__exit__` fail for any
`with redis_client.lock(...)` block. The established workaround, used by 5a in
`tests/test_inbox_dispatch_votes.py`, is a narrower `app.redis_client` double
covering only `.lock(...)`, as a genuine no-op context manager (see that file's
own fixture and its docstring). Do not change the `redis_double` fixture — it
patches the right attribute; the limitation is in the fakeredis version.

**Sessions.** `db_session` truncates rather than rolling back, so rows committed
by a test are visible to the dispatcher's own independent task session. This is
what makes seeded state reachable through `get_task_session()`.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped *separately*, and each must be killed by a distinct named test. A kill
by `respx.models.AllMockedAssertionError` is an infrastructure kill, not a
behavioural one, and does not count — re-run with the fetch served before
concluding a guard is load-bearing.

**No vacuous assertions.** An assertion on a column that already equals its
declared default proves nothing. Seed an explicit contrary baseline first. This
rule exists because 5b Task 6 shipped exactly that mistake.

## New test files and factories

Three new files, split by sub-type affinity rather than evenly by size:

- `tests/test_inbox_dispatch_undo_follow.py` — Undo/Follow's three target types
  and its unfound-target failure
- `tests/test_inbox_dispatch_undo_content.py` — Undo/Delete, Like/Dislike,
  Announce, ChooseAnswer, and the `'Unmatched activity'` fall-through
- `tests/test_inbox_dispatch_undo_moderation.py` — Undo/Lock and Undo/Block

`tests/factories.py` gains `make_chat_message(sender, recipient, ap_id, deleted=False)`.
Undo/Delete's PM-restore branch queries `ChatMessage` by `ap_id` and `sender_id`,
and no factory builds one today. `ChatMessage` (`app/models.py:283`) carries
`sender_id`, `recipient_id`, `conversation_id`, `body`, `ap_id` and `deleted`;
`conversation_id` is nullable, so the factory need not build a `Conversation`
unless a test asserts on one. Every other row this slice needs already has a
factory: `make_follow` covers `UserFollower` for Undo/Follow's user branch,
`make_instance_ban` and `ban_user_from_community` cover Undo/Block's starting
state, and `make_community_member` / `make_feed_member` with their join-request
counterparts cover Undo/Follow.

## Global constraints

- No production change outside the four fixes named above. Everything else found
  is registered, not corrected.
- Each fix is a separate commit from any test-only commit, and is proved by a
  mutation that fails a named test.
- Findings are numbered from **D104**, and the register's index note is updated
  in the same change that takes the numbers.
- The coverage floor rises to the measured blended figure rounded down, and only
  ever rises.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass; `./run_tests.sh` resets the test database itself
  when TRUNCATE churn has made it slow, and `pytest.ini`'s `session_timeout = 600`
  fails a run that exceeds ten minutes rather than letting it be waited out.

## Success criteria

1. All seven sub-types and the fall-through reach full statement coverage.
2. No guard in the arm survives either conjunct being dropped.
3. The four fixes are landed, each with a mutation-proved test.
4. `coverage_floors.ini` raised for `app/activitypub/routes.py`.
5. The findings register carries every defect found, from D104.
6. Full suite green.

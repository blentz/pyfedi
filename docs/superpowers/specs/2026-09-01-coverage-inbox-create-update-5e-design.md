# Sub-project 5e: the inbox dispatcher's Create/Update arm

**Date:** 2026-09-01
**Module:** `app/activitypub/routes.py`, the `Create`/`Update` arm of `process_inbox_request`
**Predecessors:** 5a (preamble, Announce unwrap, vote arms, Flag/Move/QuoteRequest),
5b (Follow/Accept/Reject), 5c (Delete/Lock/Add/Remove/Block), 5d (Undo, seven sub-types)
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D113**

## The unit, and why this slice finishes something

`process_inbox_request` is the campaign's central target. After 5d it stands at
**93.5%** — 725 of 775 statements — with **50 statements still uncovered, 43 of
them in one arm**, `Create`/`Update`.

The arm runs from `if core_activity['type'] == 'Create' or core_activity['type'] == 'Update':`
to the `return` immediately before `if core_activity['type'] == 'Delete':`. At
the time of writing that is `routes.py:1193-1263`: **58 statements, 15
executed (25.9%)**.

The other seven uncovered statements are elsewhere in the function, and this
slice takes six of them so that finishing the arm finishes the function:

| Lines | What | Left by |
|---|---|---|
| `:888-889` | the preamble's `'Unexpected activity from Group'` refusal | 5a |
| `:1341-1342` | the `PollVote` arm's body — `process_poll_vote` and its return | 5a covered the arm's `if`, not its body |
| `:1345-1346` | the `ChooseAnswer` arm's body — `process_question_answer` and its return | same |

**The seventh is a decision for the project owner, not for this slice.**
`routes.py:860` is `pass`, inside
`if actor_id and actor_id.startswith('https://s.rimu.geek.nz'):`, carrying the
comment *"just here to set breakpoints on, during testing. remove before
commit"*. It is debug scaffolding whose own comment says it should not be in
the tree, and it is reachable only by an activity from one named personal
domain. Covering it would mean seeding an actor at that domain purely to
execute a `pass`, which entrenches code the author meant to delete. This spec
therefore **registers it and recommends deletion, and does not cover it** —
see success criterion 4, which states the exception rather than hiding it. If
the owner authorises the deletion, the function reaches 100% instead.

### Citations drift

5c's Task 4 shifted every line after `:1300`, and 5d's fixes shifted the Undo
arm. This spec's line numbers are correct as of writing and will not stay
correct. **Locate every target by code content.** Quoted lines are
navigational aids, never identifiers.

## Branch inventory

Derived from current source, not from any earlier document.

| Branch | Selected by | Outcome |
|---|---|---|
| Unsigned string object | `isinstance(core_activity['object'], str)` | `verify_object_from_source`; on refusal, FAILURE naming the reason, and return |
| Chat message | `object['type'] == 'ChatMessage'` | `process_chat`, return |
| Poll vote | `type == 'Note'` **and** `'name' in object` **and** `'inReplyTo' in object` **and** `'attributedTo' in object` **and** `'published' not in object` | resolve post → poll → choice; on full success, `vote_for_choice`, SUCCESS log, and for a local author an `edited_at` stamp plus `task_selector('edit_post', ...)`. **Returns unconditionally.** |
| Community resolution | `not announced and not community` | `find_community`; if none, `process_chat` (return if truthy); `ensure_domains_match` else FAILURE; `community.local_only` else FAILURE |
| New content | `object_type in ['Page', 'Article', 'Link', 'Note', 'Question', 'Event']` | `process_new_content`, return |
| PeerTube video edit | `object_type == 'Video'` | post found and owned → `update_post_from_activity` + SUCCESS; found but not owned → FAILURE `'Edit attempt denied'`; not found → FAILURE `'PeerTube post not found'` |
| Community profile update | `object_type == 'Group' and type == 'Update'` | local community edited by a non-moderator → FAILURE; otherwise `refresh_community_profile` + SUCCESS, and for a local community `announce_activity_to_followers` |
| Anything else | fallthrough | FAILURE `'Unacceptable type (create): ' + object_type` |

Note the poll-vote guard is a **five-way conjunction**: one type equality,
three positive membership tests, and one negative. Each conjunct must be killed
separately, which makes it the largest single guard this campaign has tested.

## Goal

Full statement coverage of the arm and of the preamble's Group path, and branch
coverage sufficient that no guard survives having any one of its conjuncts
dropped. Expected effect: `app/activitypub/routes.py` moves from its measured
**59.4465%** blended toward **62%**, the floor rises to the measured figure
rounded down, and `process_inbox_request` reaches full statement coverage.

## Out of scope

The delegates themselves — `process_new_content`, `process_chat`,
`verify_object_from_source`, `refresh_community_profile`,
`update_post_from_activity`, `find_community`, `ensure_domains_match` — are
doubled here, not tested here. Several are large enough to deserve their own
slices later. The ActivityPub collection endpoints (`community_outbox`,
`community_featured`, `community_moderators_route`, `community_followers`),
which hold the largest remaining uncovered blocks in this module, keep their
own future spec.

## The defects this slice must confront

Three are visible on reading. This slice carries the same bounded
fix authorisation 5c and 5d had: **defects found are fixed test-first, each in
its own commit, separate from every test-only commit, and each proved by a
mutation that fails a named test.** Anything discovered that is larger than
this arm gets registered instead, as 5d did with the sibling `Lock` arm.

### An Announce from a feed is a crash surface

This is the serious one, and it is not local to this arm.

In the preamble's Announce handling, the inner actor is resolved only
`if not feed:`. When the Announce's outer actor resolved to a **feed**, the
`else` sets `user = None`, and `community` was never set either — while
`announced` becomes `True`.

The Create/Update arm guards its entire resolution chain with
`if not announced and not community:`. With `announced` true that chain is
skipped, so `community` stays `None` all the way to the `object_type` dispatch.
Three consequences, all reachable:

- the `Group`/`Update` branch calls `community.is_local()` → `AttributeError`
- the poll-vote path calls `user.id` → `AttributeError`
- `process_new_content(user, community, ...)` is handed two `None`s

Whether a feed Announces `Create` activities in practice is a separate
question from whether the code path exists; it does, and it raises rather than
logging. **The fix must be established by test before being written**, and the
tests must pin what actually happens today rather than what this spec predicts.

### The poll-vote path is silent on three of its four outcomes

`post_being_replied_to` not found, `poll_data` not found, `choice` not found —
each falls through to a bare `return` with no `log_incoming_ap` call anywhere.
Only the full-success path logs. An operator reading `ActivityPubLog` cannot
distinguish a poll vote that was recorded from one that was dropped. Same class
as D81, D82, D83 and D86.

### The poll-vote block returns unconditionally

Its `return` sits outside every inner `if`, so an activity that merely matches
the poll *shape* — a `Note` with a `name`, an `inReplyTo`, an `attributedTo`
and no `published` — is consumed even when nothing resolves, rather than
falling through to ordinary content handling. Combined with the silence above,
such an activity vanishes without trace. Whether that is deliberate is a
judgement the implementation must record either way.

## Testing approach

Unchanged from 5a-5d, restated so no task brief must rediscover it.

**Entry.** `dispatch(activity, store_ap_json=True)` from
`tests/test_inbox_dispatch_preamble.py` calls `process_inbox_request`
directly — production's own `current_app.debug` branch, not a test-only
shortcut.

**Doubling at the binding site.** `record_moderation(monkeypatch, *names)` from
`tests/test_inbox_dispatch_lock_delete.py` patches each name on
`app.activitypub.routes`. Verified for this arm: `verify_object_from_source`,
`find_community`, `ensure_domains_match`, `update_post_from_activity`,
`refresh_community_profile` and `task_selector` are imported into routes by
name; `process_chat` and `process_new_content` are defined there. Both shapes
patch identically.

**Never double `find_actor_or_create_cached` unconditionally.** The preamble
resolves the activity's own signed outer actor through it before any arm runs,
so a blanket double fails the preamble instead of reaching the branch under
test. Scope any such double to a single URL. 5d hit this twice; it is now
recorded as harness fact 17 in `tests/README.md`.

**Sessions.** `db_session` truncates rather than rolling back, so rows a test
commits are visible to the dispatcher's independent task session.

**One pytest session at a time.** The suite assumes exclusive access to the
test stack; two concurrent runs corrupt and deadlock each other. Harness fact
18 in `tests/README.md`.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped *separately*, each killed by a distinct named test. The poll-vote
guard's five conjuncts need five distinct kills. A kill by
`respx.models.AllMockedAssertionError` is an infrastructure kill, not a
behavioural one.

**No vacuous assertions.** An assertion on a column that already equals its
declared default proves nothing; seed an explicit contrary baseline first.

**Docstrings must be true.** 5d lost three fix rounds to docstrings asserting
things about sibling tests that were false. A claim about another test is
verified before it is written.

## New test files and factories

One new file, `tests/test_inbox_dispatch_create_update.py`, covering the whole
arm. The arm is 58 statements — smaller than any single file 5c or 5d
produced — and its branches share the same seeding, so splitting it would
duplicate fixtures without giving a reviewer a cleaner surface.

`tests/factories.py` gains two factories. `Poll` is keyed by `post_id` as its
primary key (`app/models.py:3744-3751`), which is why the arm looks it up with
`.get(post.id)`; `PollChoice` carries its own `id` plus a `post_id` and a
`choice_text` (`app/models.py:3783-3790`), and the arm matches a choice by
`choice_text`:

- `make_poll(post, *, mode='single', local_only=False, end_poll=None) -> Poll`
- `make_poll_choice(post, choice_text, *, sort_order=0) -> PollChoice`

`make_chat_message` already exists from 5d and serves the `ChatMessage` branch.
Everything else the arm needs — communities, feeds, users, posts — has a
factory.

## Global constraints

- Defects found in this arm are fixed test-first, each in its own commit,
  separate from every test-only commit, each proved by mutation. Anything
  outside the arm is registered, not fixed.
- Findings are numbered from **D113**, and the register's index note is updated
  in the same change that takes the numbers.
- The coverage floor rises to the measured blended figure rounded down, and
  only ever rises.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, and only one pytest
  session runs at a time.

## Success criteria

1. Every branch in the inventory above reaches full statement coverage, plus
   the preamble's `'Unexpected activity from Group'` path and the `PollVote`
   and `ChooseAnswer` arm bodies.
2. No guard in the arm survives any one conjunct being dropped — five distinct
   kills for the poll-vote guard alone.
3. Defects found are either fixed with a mutation-proved test, or registered
   with a stated reason for not fixing.
4. `process_inbox_request` reaches full statement coverage **except
   `routes.py:860`**, the debug `pass` described above, which is registered
   with a recommendation to delete rather than covered. If the owner authorises
   deleting it, this criterion becomes unconditional.
5. `coverage_floors.ini` raised for `app/activitypub/routes.py`.
6. The findings register carries every defect found, from D113.
7. Full suite green.

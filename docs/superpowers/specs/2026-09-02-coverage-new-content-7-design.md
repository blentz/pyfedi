# Sub-project 7: federated content ingestion

**Date:** 2026-09-02
**Module:** `app/activitypub/routes.py`, the function `process_new_content`
**Predecessors:** 5a-5e brought `process_inbox_request` to all but one statement;
sub-project 6 covered `process_chat`, the first of its delegates. This is the second.
**Findings register:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`,
continuing at **D132**

## The unit

`process_new_content(user, community, store_ap_json, request_json, announced)` —
at the time of writing `routes.py:2296-2411`, **87 statements, 1 executed
(1.1%)**. It is the largest single uncovered function left in the module.

It is where **every federated post and comment actually lands**. The
Create/Update arm resolves the actor and community and then hands off here;
sub-project 5e covered that hand-off by doubling this function. Everything it
does — permission, creation, editing, refusal, and telling the remote instance
its content was rejected — is untested.

### Citations drift

Every sub-project since 5c found its brief's line numbers stale, and this
slice's own fixes will move things. **Locate every target by code content.**
Quoted lines are navigational aids, never identifiers.

## What it does

A preamble, then **two symmetric halves**.

**Preamble.** For a direct activity it reads `inReplyTo` and `id` from
`request_json['object']`; for an announced one, from
`request_json['object']['object']`, and sets `announce_id` from the outer id.
It then truncates `activity_json['id']` to 100 characters, and if `community`
is `None` falls back to `find_microblogging_community()`.

**Post half** (`not in_reply_to`) and **reply half** (`in_reply_to`), each with
the same shape:

| Case | Post half | Reply half |
|---|---|---|
| Object exists, activity is `Create` | FAILURE `'Create processed after Update'` | same |
| Object exists, editor permitted | `update_post_from_activity` + SUCCESS + announce when not announced | `update_post_reply_from_activity`, **but only if `can_create_post_reply`** |
| Object exists, editor not permitted | FAILURE `'Edit attempt denied'` | same |
| New object, user may create | `create_post`; on success an Update-lost-the-race check, then SUCCESS + announce | `create_post_reply`, same shape |
| New object, delegate refused it | `proactively_delete_content` when the community is local | same |
| `TypeError` from the create delegate | FAILURE `'TypeError. See log file.'` | same |
| User may not create at all | proactive delete + FAILURE `'User cannot create post in Community'` | FAILURE `'User cannot create reply in Community'` |

**Delegates**, all imported into `app.activitypub.routes` and therefore
patchable there:

```python
can_create_post(user, content: Community) -> bool                 # app/utils.py:2479
can_create_post_reply(user, content: Community) -> bool           # app/utils.py:2531
create_post(store_ap_json, community, request_json, user, announce_id=None) -> Post|None   # util.py:2753
create_post_reply(store_ap_json, community, in_reply_to, request_json, user, announce_id=None)  # util.py:2572
update_post_from_activity(post, request_json)                     # util.py:3109
update_post_reply_from_activity(reply, request_json)              # util.py:2987
proactively_delete_content(community: Community, ap_id: str)      # util.py:4675
find_microblogging_community()                                    # util.py:4607
announce_activity_to_followers(community, creator, activity, can_batch=False, ...)  # DEFINED in routes.py:1946
```

Note `can_create_post`'s second parameter is named `content`, not `community`.

## Goal

Full statement coverage of `process_new_content`, and branch coverage
sufficient that no guard survives having any one of its conjuncts dropped —
the post half's three-way permission disjunction being the largest. Expected
effect: `app/activitypub/routes.py` moves from its measured **66.4223%**
blended toward **70%**, and the floor rises to the measured figure rounded
down.

## Out of scope

The delegates themselves. `create_post` and `create_post_reply` are each large
enough to deserve their own slice and are doubled here, not tested here — a
test that drove real post creation would be testing them, not this function.
The remaining uncovered functions in this module — `process_webfinger_request`
(42 statements), `community_profile` (41), `feed_profile` (31),
`announce_activity_to_followers` (29) — keep their own future specs.

## The defects this slice must confront

Four are visible on reading, and three of them are **asymmetries between the
two halves** — which is what makes them credible as defects rather than as
deliberate design. This slice carries the same bounded fix authorisation 5c
through 6 had: **defects found are fixed test-first, each in its own commit,
separate from every test-only commit, and each proved by a mutation that fails
a named test.** Anything larger than this function is registered.

### An instance admin can edit a federated post but not a reply

The post half permits an edit when
`user.id == post.user_id or post.community.is_moderator(user) or post.community.is_instance_admin(user)`.
The reply half permits it when
`user.id == reply.user_id or reply.community.is_moderator(user)` — the third
disjunct is simply absent. An instance admin editing a federated comment is
refused with `'Edit attempt denied'`.

Nothing in the surrounding code suggests the distinction is intended, and the
two halves are otherwise written from each other.

### A permitted reply edit that fails `can_create_post_reply` returns silently

In the reply half:

```python
if user.id == reply.user_id or reply.community.is_moderator(user):
    if can_create_post_reply(user, community):
        update_post_reply_from_activity(reply, activity_json)
        log_incoming_ap(id, APLOG_UPDATE, APLOG_SUCCESS, saved_json)
        if not announced:
            announce_activity_to_followers(reply.community, reply.author, request_json)
    return
```

When `can_create_post_reply` is false the bare `return` fires with **no log on
any path**. The post half has no equivalent inner check at all, so this is both
a silent drop and an asymmetry — an editor who is permitted by the outer check
but banned from the community gets no record either way.

### The post half's "creation refused" branch neither logs nor returns

At `routes.py:2349-2351`:

```python
else:  # The post was not allowed - send a 'Delete' to remove it from the remote instance
    if community.is_local():
        proactively_delete_content(community, ap_id)
except TypeError:
```

After the proactive delete there is no `log_incoming_ap` and no `return`.
Control leaves the `try` without the `except` firing, leaves
`if can_create_post(...)`, leaves `if not in_reply_to:`, and falls off the end
of the function, returning `None` with nothing written to `ActivityPubLog`.

The reply half's mirror at least `return`s explicitly. **Neither logs** — an
operator cannot tell a refused-and-deleted post from one that was never
received. Verified at source rather than inferred.

### `activity_json['id']` is truncated in place, mutating the caller's data

```python
activity_json['id'] = shorten_string(activity_json['id'], 100)
```

For an announced activity `activity_json` **is** `request_json['object']`, and
for a direct one `activity_json` **is** `request_json` itself — either way this
mutates a dict the caller still holds. The comment above the line says *"Not
referred to again, so it shouldn't matter if they're truncated"* — which is not
true of `request_json`.

**Corrected during implementation.** An earlier draft of this section claimed
the truncated id reaches followers because `request_json` is passed to
`announce_activity_to_followers`. That is false as stated: all four call sites
in this function (`routes.py:2332, 2350, 2377, 2398`) sit inside `if not
announced:`, so on the *announced* path — the one where `activity_json` is a
caller's nested dict — none is reachable.
Follower propagation is a **direct-path** consequence, where `activity_json is
request_json` and the guard does hold.

**The truncation itself is load-bearing and must survive any fix.** The
already-truncated `activity_json` is forwarded into `create_post` /
`create_post_reply` and stored by `Post.new` / `PostReply.new` as `ap_create_id`
(`app/models.py:1861`, `:2968`) — a `db.String(100)` column on both models
(`:1722`, `:2893`). That width is what the comment's "will crash the app" refers
to. A fix must remove the write-back into the caller's structure while keeping
the truncated value flowing to storage.

Whether the truncation itself is right is a separate question the tests should
answer rather than assume; the defect registered here is the in-place mutation
of a caller's structure and the comment that denies it.

### Also present, registered rather than fixed

`find_microblogging_community()` can return `None`, after which `community` is
dereferenced unguarded throughout both halves (`community.is_local()`,
`can_create_post(user, community)`). Whether it can actually return `None` in a
reachable configuration is a question the tests should answer before any code
changes — the same reasoning that kept `sender.instance.trusted` unfixed in
sub-project 6.

`if user.user_name == 'rimu': pass` at the top is already registered as **D118**
by sub-project 5e, with a recommendation to delete. Not re-registered here.

## Testing approach

Unchanged from 5a-6, restated so no task brief must rediscover it.

**Entry.** `dispatch(activity, store_ap_json=True)` from
`tests/test_inbox_dispatch_preamble.py`, with a `Page`/`Note`-typed inner
object that reaches the Create/Update arm's `process_new_content` call. Driving
it through the dispatcher rather than calling it directly is what supplies a
real `announced` flag and a real task session.

**The announced and direct shapes read different nesting levels**, so both must
be driven end to end rather than simulated — the preamble's `else` branch is
where `announce_id` comes from, and it is the only place the outer id is used.

**Doubling at the binding site.** `record_moderation(monkeypatch, *names)` from
`tests/test_inbox_dispatch_lock_delete.py` patches names on
`app.activitypub.routes`. All eight delegates listed above are imported there;
`announce_activity_to_followers` is defined there. Both shapes patch alike.

**`create_post` and `create_post_reply` must be doubled in every test.** They
are large, they write many rows, and letting them run would make these tests
about them instead. Their return value — a `Post`/`PostReply` or `None` — is
the switch this function branches on, so each test chooses it deliberately.

**Never double `find_actor_or_create_cached` unconditionally.** The preamble
resolves the activity's own signed actor through it before any arm runs.
`tests/README.md` fact 17; two sub-projects hit it.

**Seeding.** `seed_community_owner(domain)` runs before `make_community(host=domain)`
— the factory hardcodes `user_id=1`/`instance_id=1` against real foreign keys,
and two tasks in 5e hit that. `make_community_member(user, community, is_moderator=True)`
grants moderator status. `make_community` never sets `ap_id`, so every factory
community is `is_local() == True` unless a test sets it — `tests/README.md`
fact 20, which cost 5e a fix round.

**Mutation discipline.** Every guard is mutation-tested with each conjunct
dropped *separately*, each killed by a distinct named test. The post half's
permission check is a three-way disjunction and needs three kills; the
Update-lost-the-race checks are two-way conjunctions. A kill by
`respx.models.AllMockedAssertionError` is an infrastructure kill, not a
behavioural one.

**No vacuous assertions.** An assertion on a column that already equals its
declared default proves nothing. `Post.edited_at` is `None` by default and the
lost-race check turns on exactly that, so a test of the "not a lost race" side
must seed it non-`None`.

**Docstrings must be true.** A claim about what another test proves is verified
before it is written; sub-projects 5d, 5e and 6 each lost fix rounds to this.

**One pytest session at a time.** `tests/README.md` fact 18.

## New test file

One new file, `tests/test_inbox_dispatch_new_content.py`. At 87 statements
across two symmetric halves this is the largest unit the campaign has taken in
one slice, but splitting it would duplicate the whole seeding surface — both
halves need the same community, actor and activity shapes — and would give a
reviewer two files that only make sense read together.

No new factories are expected: posts, replies, communities, users and
memberships all have them. The plan confirms this against the models rather
than assuming it.

## Global constraints

- Defects found in `process_new_content` are fixed test-first, each in its own
  commit, each proved by mutation. Anything outside the function is registered.
- Findings are numbered from **D132**, and the register's index note is updated
  in the same change that takes the numbers.
- The coverage floor rises to the measured blended figure rounded down.
- Locate every code target by content, not by the line numbers in this spec.
- The full suite must pass. Only the controller runs it, one session at a time.
- **Delete nothing** that the task did not create. Agents in sub-project 6
  destroyed an unrelated untracked file three times.

## Success criteria

1. `process_new_content` reaches full statement coverage.
2. No guard survives any one conjunct being dropped — three distinct kills for
   the post half's permission disjunction alone.
3. Both the announced and direct preamble shapes are driven end to end.
4. Defects found are fixed with a mutation-proved test, or registered with a
   stated reason for not fixing.
5. `coverage_floors.ini` raised for `app/activitypub/routes.py`.
6. The findings register carries every defect found, from D132.
7. Full suite green.

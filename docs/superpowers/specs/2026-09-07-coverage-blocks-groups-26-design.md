# Sub-project 26: closing `__init__.py`, `groups.py` and `blocks.py`

Design document. Branch `blentz`. Follows sub-project 25, which closed
`app/shared/tasks/locks.py` and `app/shared/tasks/likes.py` to 100.0% each
(23 commits, `ced6438f..60416a91`).

## 1. Scope, and why these three

Three modules, 178 statements and 58 branches. Chosen together because they
close two things at once that no other combination does:

- **D309 drops to a single remaining file.** `blocks.py:104` and
  `groups.py:59` are two of its last three sites; closing both leaves only
  `deletes.py:127`/`:130`.
- **`task_selector` gets closed** — the dispatcher every task in the package
  routes through, currently at 83.3333% on **two missing statements**. It is
  the one place a task-key typo or a `send_async` regression would surface,
  and it has never been covered directly.

The three are worked in ascending difficulty, and the order is deliberate:
`__init__.py` (2 statements), then `groups.py` (54), then `blocks.py` (104).

**These two D309 sites are not like the eight already closed.** Every previous
site read `local_only or not instance.online()` and was missing only `private`.
`blocks.py:104` and `groups.py:59` both read:

```python
    if community.local_only:
        return
```

They omit `private` **and** the `online()` check. Each fix therefore adds two
conjuncts, not one, and the register must record that these were wider gaps
than the family's other members.

## 2. Baselines

Measured from `scratch_full_cov.json` at `60416a91` (mtime 2026-09-07 19:44:34).
Re-derive before starting.

| Module | Lines | Statements | Branches | Partial | Missing | Percent |
|---|---|---|---|---|---|---|
| `app/shared/tasks/__init__.py` | — | 20 | 4 | 2 | 2 (`:63`, `:68`) | 83.3333% |
| `app/shared/tasks/groups.py` | 149 | 54 | 24 | 0 | 44 | 12.8205% |
| `app/shared/tasks/blocks.py` | 190 | 104 | 30 | 0 | 88 | 11.9403% |

Coverage takes the **dotted module form**. The JSON lands *inside* the
`pyfedi_test-runner` container; retrieve it with `podman cp` (fact 137).

## 3. Phase A — `task_selector` (`app/shared/tasks/__init__.py`)

```
:61  def task_selector(task_key, send_async=True, **kwargs)
:62      if current_app.debug:
:63          send_async = False          <- MISSING
:65      if send_async:
:66          tasks[task_key].delay(send_async=send_async, **kwargs)
:67      else:
:68          return tasks[task_key](send_async=send_async, **kwargs)   <- MISSING
```

Two missing statements and two partial arcs, `(62,63)` and `(65,68)`.

**Why they are missing, and what makes each reachable.** `tests/conftest.py`
sets `task_always_eager`, so `.delay()` at `:66` already executes
synchronously — which is exactly why `:66` is covered and `:68` is not. And
`current_app.debug` is False under test, so `:63` never runs.

- `:63` is reached by setting `current_app.debug` True for the call.
- `:68` is reached by calling with `send_async=False`.

**`:68` HAS A DISCRIMINATOR `:66` DOES NOT, and the tests must use it.** `:66`
falls off the end and returns `None`; `:68` **returns the task's return
value**. So asserting a non-`None` return proves `:68` ran rather than `:66`.
Asserting only that the task executed would pass under either arm, because
`task_always_eager` makes both synchronous — that is the vacuous shape to
avoid here.

Pick a task key whose function returns something observable, or assert the
identity of what comes back. Read `tasks` dict at `:33-59` and choose
deliberately; do not assume any given task returns a value.

Phase A ends with `__init__.py` measured and floored before Phase B begins.

## 4. Phase B — `groups.py`

149 lines, one function: `edit_community(send_async, user_id, community_id)` at
`:52`.

```
:54  session = get_task_session()
:57  user = session.query(User).get(user_id)
:58  community = session.query(Community).filter_by(id=community_id).one()
:59  if community.local_only: return          <- D309, production change 2
:62  if not community.is_moderator(user): return
:68-114  the Group envelope, mostly conditional dict-building
:116     group["tag"] = community.flair_for_ap(version=2)
:120-128 the Update wrapper
:129     if community.is_local():  -> Announce, delivered per following instance
:143-144 else: send the Update straight to community.ap_inbox_url
```

### 4.1 The branch-dense envelope

24 branches in 54 statements — the highest ratio of any module this campaign
has closed. Most come from `:85-113`, where each of `description_html`,
`description`, `icon_id` and `image_id` is optional, and `icon_id`/`image_id`
each branch **again** on whether the stored image url starts with `http`:

```
:89  if community.icon_id:
:90      if community.icon_image().startswith('http'):   -> absolute url
:95      else:                                            -> SERVER_URL + path
```

Both arms of both image branches need covering, which means four fixtures
differing only in one column. Prefer one test per arm with a docstring naming
the arm, over a parametrised sweep — a parametrised failure names the parameter
rather than the branch.

### 4.2 The two apparently unused imports

`:2` imports `post_request` alongside `default_context` and
`send_post_request`; only the latter two are called. `:5` imports
`get_comm_flair_list` and `comm_flair_ap_format` from `app.shared.community`;
neither appears anywhere in the file — `:116` calls
`community.flair_for_ap(version=2)`, a model method, instead.

Verify both claims by grep before recording them. This is the same shape as
sub-project 23's finding that `post_request` is imported and unused in both
twins, so it extends an existing register entry rather than opening a new one —
**read the register and find which**.

### 4.3 The `is_moderator` gate

`:62` returns when the acting user is not a moderator. That is a second early
return with its own arm, and it is reached only after `:59`'s gate passes, so
its test needs a community that is neither `local_only` nor (post-fix) private,
with a non-moderator user. `Community.is_moderator` is a model method — read it
before building the fixture rather than assuming what makes it False.

## 5. Phase C — `blocks.py`

190 lines. Four `@celery.task` wrappers delegating to one handler:

```
:41  ban_from_site(...)        -> ban_person(..., community_id=None, ...)
:55  unban_from_site(...)      -> ban_person(..., None, ..., is_undo=True)
:69  ban_from_community(...)   -> ban_person(..., community_id, ...)
:83  unban_from_community(...) -> ban_person(..., community_id, ..., is_undo=True)
:96  ban_person(session, user_id, mod_id, community_id, expiry, reason,
              remove_data, is_undo=False)
```

### 5.1 The structural fork at `:101`

`ban_person` branches on `community_id` before building anything:

- **community ban** (`:102-107`): loads the community, sets `communities` to
  `[community]` only if it is local, returns at `:104` if `local_only`, and
  addresses `cc`/`target` at the community.
- **instance ban** (`:108-112`): `community = None`, `communities = []`,
  `cc = []`, and `target` is the server root.

That fork produces two different envelopes from one handler, and `:130-133`
forks again on the same condition to set `audience`. Both need covering in both
`is_undo` states — four combinations before any delivery path is considered.

### 5.2 Three delivery paths, and only one of them uses `following_instances()`

- `:158-163` **site ban**: iterates
  `session.query(Instance).filter(Instance.software != 'mastodon').all()` —
  a raw query, not `following_instances()` — guarded inline by
  `instance.inbox and instance.online() and instance.id != 1`, then `return`s.
  Note it applies the dormant/gone-forever filter *manually* via `online()`,
  which is why this is **not** another D321. Record the distinction.
- `:166-168` **remote community**: one direct send to `ap_inbox_url`, then
  `return`.
- `:172-190` **local communities**: an Announce per community in `communities`,
  delivered to each `following_instances()` row that passes
  `instance.inbox and instance.online()`.

### 5.3 The fallback send at `:189-190` — production change 3

```python
        if user.instance_id not in sent_to:     # <comment explaining why>
            send_post_request(user.instance.inbox, announce, ...)
```

`:187` guards every other recipient with `instance.inbox and
instance.online()`. This fallback, three lines later, guards nothing.

**Two distinct failure modes, and the fix must handle both:**

1. `User.instance_id` is a nullable FK (`app/models.py:54` in the `User`
   class — re-derive) and `User.instance` is a `lazy='joined'` relationship.
   If `instance_id` is None, then `None not in sent_to` is True and
   `user.instance.inbox` raises `AttributeError` on None.
2. If the instance row exists but has a null `inbox`, the send goes to `None` —
   which `post_request` records as an `empty uri` failure row without making a
   request.

**The comment at `:189` explains why the fallback exists** — `following_instances()`
excludes instances whose only follower was the person just banned, so their
home instance must still be told. The fix must not break that case. Mirror
`:187`'s guard while preserving the `not in sent_to` condition:

```python
        if user.instance_id not in sent_to and user.instance and user.instance.inbox and user.instance.online():
```

**KEEP IT ON ONE LINE, and preserve the trailing comment.** `:189` is already
**225 characters** — the explanatory comment lives on it — and `:188` and `:190`
are 120 and 121. The repository has no linter configuration, so a 112-character
condition plus that comment is comfortably within what this file already does.
Wrapping it would turn a one-line modification into a two-line insertion and
change the file's length, which every mutation restore in this sub-project has
to assert against.

Re-derive the line and its indentation. This is a modification, not an
insertion, so `blocks.py` stays **190 lines** and the whole file's numstat for
this sub-project is `2 2` — one line for this guard, one for the `:104` gate.

### 5.4 The dead `@context` assignment — registered, not fixed

`:143` sets `undo['@context']` and `:151` deletes it unconditionally eight
lines later. Harmless: on the site-ban and remote-community paths the Undo is
the top-level object and `app/activitypub/signature.py:100-101` reinjects an
identical value; on the local path it is nested inside an Announce, where its
absence is correct. So the assignment is dead work with no observable effect.

**Record it, do not fix it.** It is the fourth production change and the user
scoped three. Note in the entry that this is *why* it is invisible — a reader
who removes `:143` will see no test fail, and should know that is expected
rather than a coverage gap.

## 6. Production changes — three

| # | Site | Change | Register |
|---|---|---|---|
| 1 | `blocks.py:104` | `local_only` → `local_only or private or not instance.online()` | D309, **two conjuncts added** |
| 2 | `groups.py:59` | same shape | D309 → one remaining file |
| 3 | `blocks.py:189-190` | guard the fallback send on `user.instance`, its inbox, and `online()` | new number |

Changes 1 and 2 place `private` before the `online()` call, matching the
convention at eight sibling sites: `Community.instance_id` is nullable and `or`
short-circuits left to right, so the ordering means a private community with no
instance row returns at the guard rather than raising.

Each is test-first with a named pre-fix failure. Note that the pre-fix failure
for 1 and 2 will be an `ActivityPubLog` count of 1 — a **failed-delivery** row,
not a completed send, because `post_request` writes its row at
`signature.py:105` before any network attempt and marks it `failure` at
`:143-148` when the unregistered route raises. Say that correctly; three
implementers in this campaign have described that row as a successful
federation.

`app/`'s numstat for the whole sub-project should be `1 1` for `groups.py` and
`2 2` for `blocks.py`, with both files' line counts unchanged.

## 7. Findings to register

Next free number is **D328** — confirm against the register.

1. **The fallback send's missing guard** — fixed by production change 3.
   Record both failure modes and note that `following_instances()`'s exclusion
   of the just-banned user's instance is *why* the fallback exists, so the fix
   preserves it rather than removing it.
2. **`blocks.py:104` and `groups.py:59` omitted two conjuncts, not one.**
   Extend D309's cell to say so — every other site in that family was missing
   only `private`, and a reader who assumes uniformity would under-fix
   `deletes.py`, the last remaining site. **Check `deletes.py:127` and `:130`
   while you are there and record which shape they are**; that costs one grep
   and tells the next round what it is walking into.
3. **`blocks.py:143`'s dead `@context` assignment** (§5.4), with the reason its
   removal would be invisible to tests.
4. **`blocks.py:159`'s raw `Instance` query is NOT another D321.** It bypasses
   `following_instances()` but applies `online()` and `id != 1` inline, so the
   dormant/gone-forever filtering happens anyway. Record the distinction
   explicitly, because the surface shape is identical to D321's and a later
   reader pattern-matching on "raw Instance query" would file a duplicate.
5. **`groups.py`'s unused imports** — extend the existing entry that already
   records this shape for the `adds.py`/`removes.py` twins rather than
   allocating.

New harness facts, appended from **146**:

- **`task_selector`'s two arms are indistinguishable under
  `task_always_eager` except by return value.** `:66` returns None; `:68`
  returns the task's value. A test asserting only that the task executed passes
  under either.

## 8. Risks

1. **`groups.py`'s 24 branches in 54 statements** is the densest ratio the
   campaign has met. Four of them are nested image-url branches differing only
   by one fixture column; getting one arm wrong is easy and shows up as a
   partial branch rather than a failure.
2. **Production change 3 is a modification inside a loop with a comment
   explaining its own existence.** The comment is the specification. Read it
   before changing the line, and make the fix preserve the case it describes —
   a guard that also drops the just-banned user's instance would silently undo
   the behaviour the fallback exists to provide.
3. **`ban_person`'s four combinations** (community/instance × undo/not) plus
   three delivery paths give twelve shapes, not all of which are reachable.
   Enumerate which are before writing tests, and document any that are not
   rather than writing a test that pretends otherwise.
4. **Phase A is small enough to feel like a formality.** It is the dispatcher
   every task routes through; its `:68` arm is the synchronous path that
   `send_async=False` callers take in production. Give it the same
   discrimination scrutiny as a large module.

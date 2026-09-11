# Coverage sub-project 36: `post.py` Group C, author lifecycle and reporting

**Status:** approved, not yet implemented
**Branch:** `blentz`
**Predecessor:** sub-project 35, 15 commits `7b575127..47cbb5cd`, Group B closed at zero missing statements and zero missing arcs, module floor 50 → 64

## Goal

Take `delete_post`, `restore_post` and `report_post` to zero missing statements
and zero missing arcs, and fix two production defects found while reading them.

This is the third of five sub-projects against `app/shared/post.py`. It does not
close the module — Groups D and E remain.

## Scope

101 statements and 56 missing branch arcs, measured against the full suite at
commit `47cbb5cd` (4544 passed, 3 skipped, exit 0) on the current 1193-line
tree:

| Function | Lines | Missing stmts | Missing arcs |
|---|---|---|---|
| `report_post` | `:821-926` | 57 | 36 |
| `delete_post` | `:755-795` | 28 | 14 |
| `restore_post` | `:796-820` | 16 | 6 |

Counts are **decorator-inclusive**, this campaign's convention, and were
measured against the FULL suite. The module stands at **64.845%** against a
floor of 64 — 0.845 points of headroom.

The total matches D392's decomposition exactly — 101 and 56 — for the third
round running. Group C's range is untouched by sub-projects 34 and 35, whose
production changes all landed at or below `:927`.

**`report_post` alone is 57 of the 101 statements and 36 of the 56 arcs.** It is
the round's centre of gravity and the reason the task split is uneven.

Roughly 60 tests, against Group B's 65 for 110 statements and 56 arcs.

## The harness is inherited and needs no new construction

Sub-projects 34 and 35 built and consolidated it. `tests/factories.py` holds
`seed_post_context`, `web_ctx` and `bearer`; `tests/README.md` facts 206-219
carry the rules. Four bind hardest here:

- **No test may request `redis_double`.** `delete_post:765` is
  `from app import redis_client` inside the function body and `:766` locks on
  it — the exact shape that rule exists for. The fixture reaches the
  function-body import and then breaks the lock release on `EVALSHA`, which
  fakeredis does not implement.
- **`s.author` is User id 1 and an unconditional site admin.** That has been a
  trap for two rounds; here it is worse than a trap. `report_post:893` iterates
  `Site.admins()`, so the seeded author is not a neutral bystander — it changes
  the notification count every reporting test asserts on. **Tests must reason
  about who is an admin, not merely avoid `s.author` as an actor.**
- **SRC_API arms need no request context** — `get_ip_address` swallows the
  missing-context `RuntimeError`.
- **`grant_permission` cannot make a site admin**; `is_admin()` and `is_staff()`
  check role NAMES.

### Two things Task 1 must probe rather than assume

**1. Does `report_post` need a request context on the SRC_API arm?** `:877`
enters `with force_locale(get_recipient_language(moderator.id)):` and `:878`
calls `gettext`. Babel's locale machinery may require a request or app context
beyond what the inherited rule covers. The rule says the SRC_API arm needs no
request context; this function may be the exception. Probe it and record the
answer.

**2. What does `Site.admins()` return under `seed_post_context`?** Every
`notify_admins` test depends on it. Probe it and record the count and identity,
because `s.author` landing on id 1 makes the obvious assumption unsafe.

### One fact new to Group C, and it is load-bearing

**`delete_post` is called from Celery tasks with no request context.**
`app/shared/tasks/maintenance.py:150` calls
`delete_post(post_id, False, SRC_WEB, None)` and `:185` calls
`delete_post(post.id, post.author.is_local(), SRC_WEB, None)`, both inside task
bodies. There, Flask-Login's `current_user` proxy resolves to `None`, so
`:760`'s `if current_user:` is False and `:763`'s `user_id = 1` fallback fires —
exactly as its comment says.

**That path is live and a test must cover it without a request context.** An
earlier draft of this spec called the fallback unreachable and proposed
"fixing" `:760` to test `current_user.is_authenticated`. Reading the callers
refuted it: the fallback is correct, and the proposed fix would have been a
change to working code. The claim is recorded here so no later round re-derives
the same wrong conclusion.

## Production changes

Two. Each lands as its own commit with its own failing observation.

**Every one must be observed failing before it is fixed.** A production change
with no failing observation behind it is the same error as a test that cannot
fail, one level up.

### PC1 — `report_post`'s minor-abuse escalation has never fired

`:828-829` reads:

```python
        notify_admins = (any(x in reason.lower() for x in ['Minor abuse', 'doxing']) or
                        any(x in description.lower() for x in ['Minor abuse', 'doxing']) or
```

The haystack is lowercased and the needle keeps its capital M, so
`'Minor abuse' in reason.lower()` is **always False**. Verified by execution:
`'Minor abuse' in 'minor abuse happened here'` returns False. `'doxing'` matches
because it is already lowercase.

The API arm mirrors the WEB arm's policy exactly, and the correspondence is
what establishes intent:

| WEB (`:838`) | API (`:828-830`) | Canonical string |
|---|---|---|
| `'5' in reasons` | `'Minor abuse'` | `Minor abuse or sexualization` (`app/post/forms.py:36`) |
| `'6' in reasons` | `'doxing'` | `Sharing personal info - doxing` (`app/post/forms.py:35`) |
| `'17' in reasons and software != 'piefed'` | `reason == 'AI content that needs flair'` | `AI content that needs flair` (`app/post/forms.py:30`) |

So `'Minor abuse'` is a correct prefix of reason 5's canonical text with correct
casing, and only the `.lower()` breaks it. `:830`'s AI check works because it
uses exact equality and never calls `.lower()`.

**Fix: lowercase the needles to `['minor abuse', 'doxing']`.** That preserves
the evident intent of case-insensitive matching. **Do NOT fix it by removing
`.lower()`** — that would make `'doxing'` stop matching a capitalised
`'Doxing'`, trading one silent failure for another.

### PC2 — `:905` tests the wrong id space

```python
903	    if report_remote:
904	        if not post.community.is_local():
905	            if post.community_id not in remote_instance_ids:
906	                remote_instance_ids.add(post.community.instance_id)
907	        if not suspect_user.is_local():
908	            if suspect_user.instance_id not in remote_instance_ids:
909	                remote_instance_ids.add(suspect_user.instance_id)
```

`:905` compares a **community** id against a set of **instance** ids, then
`:906` adds an instance id. `:908-909` performs the identical guard correctly
two lines below, testing and adding the same value — which is the in-file proof
of intent.

**Consequence: a false skip.** When a community id coincides with an instance id
already in the set, the community's instance is never added and its moderators
never receive the Flag. The ids are drawn from different sequences, so the
collision is a coincidence rather than a rule — which is precisely why it will
not show up in casual testing.

**Fix: test `post.community.instance_id`.**

## Test design

New file: `tests/test_shared_post_lifecycle.py`.

The name is close to `tests/test_shared_tasks_maintenance_lifecycle.py`, which
already exists and covers a different module. That was raised and accepted
deliberately. A `grep` for "lifecycle" will match both; the two are told apart
by their `shared_post` versus `shared_tasks` segment.

### Every test must be able to fail

The previous two rounds caught the same defect ten times between them: a
docstring claiming more than its test proves. Sub-project 35 alone hit it five
times, including a test whose oracle pinned one guard while its docstring
claimed two, and two whose docstrings named a crash site that a later change had
moved.

State per test the regression it catches and the arm it pins, and check each
docstring against the control flow the test actually takes.

Two specific traps this round inherits:

- **A notification-count assertion is a statement about `Site.admins()` as much
  as about the code under test.** If an assertion is "one notification was
  written", it silently depends on exactly one admin existing. Assert on
  identity where possible, not only on count.
- **`report_post` commits at `:912` and adds to the session throughout.** A test
  asserting that nothing was written must query the database rather than the
  session.

### Coverage per function

**`report_post` (`:821-926`)** — the source fork at `:822`; `:828-830`'s
three-disjunct compound with each disjunct exercised separately, and the
`software != 'piefed'` conjunct of the third separately again; `:838`'s
three-disjunct WEB equivalent likewise; `:841`'s `is_local() and un_moderated`
override; the moderator loop at `:873` with `:875`'s `if moderator:` on both
arms and `:876`'s local/remote fork; `:886`'s `report_remote` fork inside the
remote arm, with `:887`'s two-conjunct instance comparison; `:892`'s
`notify_admins` fork and `:894`'s `already_notified` guard on both arms;
`:903-909`'s remote-instance block including PC2's corrected guard; `:914`'s
`remote_instance_ids` truthiness; `:916`'s `description` fork; and `:921`'s
return fork.

**`delete_post` (`:755-795`)** — `:756`'s source fork; `:760`'s `current_user`
fork with the Celery-task path taking the false arm; `:768`'s `post.url` fork;
`:778`'s two-conjunct federation guard with each conjunct witnessed separately;
the notification loop at `:783` with `:785`'s two-disjunct report guard on both
arms and a zero-iteration exit; `:790`'s return fork.

**`restore_post` (`:796-820`)** — `:797`'s source fork; `:804`'s `post.url`
fork; `:815`'s return fork. Note `:813` federates unconditionally, so there is
no guard to exercise — that asymmetry is registered, not fixed.

## Verification

- **Mutation pass** over every branch point, one at a time: dry-run without `-i`
  and read the produced line, apply, run, restore, then assert an empty
  `git diff -- app/` and the expected `wc -l`. **Enumerate every site before
  mutating any.**
- **Conjunct by conjunct on every compound.** `:828-830`, `:838`, `:778`,
  `:785`, `:887` and `:841` are all multi-operand conditions that coverage.py
  records as single arc pairs.
- **A crash kill is not a kill.** Sub-project 35 recorded a mutation as killed
  whose failure was a `TypeError` from an unrelated signature mismatch. For any
  mutation whose failure is not an assertion, ask: *does a viable non-crashing
  variant of the same fault survive?*
- **A mutation operator can be structurally void.** Check that a swap has a
  signature-compatible target before reading its crash as a kill.
- **`-x` reports only the first failing arm**, so a crash kill may mask an
  assertion kill elsewhere. Re-run without `-x` where the distinction matters.
- **Mutate PC1 and PC2 specifically.** Reverting PC1's needles to their capitals
  must be caught, and so must reverting PC2's guard to `post.community_id`.
- **Coverage** measured with the dotted form `--cov=app.shared.post` and
  `--cov-branch`, across the new file and the three existing `post` test files.
  Read `summary.percent_covered`. Write the JSON outside the repository.
- **Floor** raised from 64 to the measured integer, rounded down. This round
  does not close the module.
- **Full suite** run by the controller alone. The in-container python form is
  `podman-compose -f compose.test.yaml exec -T test-runner python ...`
  (`tests/README.md:403-405`); `run_tests.sh` has no `--exec` flag.

## Register

Findings from **D437**, facts from **220**.

- The two production changes, each with its observed failure.
- **The three `delete_post`/`restore_post` asymmetries**, registered not fixed:
  `restore_post:813` federates unconditionally where `delete_post:778` guards on
  `federate_deletion and status == POST_STATUS_PUBLISHED`, so restoring a
  never-published post federates a restore no peer can match; `delete_post:774`
  bumps `post.author.last_seen` and `restore_post` does not; `delete_post` holds
  a redis lock across its mutation and `restore_post` holds none.
- **The API/WEB source-instance divergence**: `:825` uses
  `Instance.query.filter_by(id=post.instance_id).one()` and `:835` uses
  `Instance.query.get(suspect_user.instance_id)` — a different source AND a
  different missing-row behaviour, `.one()` raising where `.get()` returns None.
- **Two meanings of "source instance" in one function**: `:849` stores
  `source_instance.id` in `targets_data` while `:865` stores
  `reporter_user.instance_id` on the `Report` row.
- **The moderator/admin counter asymmetry**: `:900` bumps
  `admin.unread_notifications` where the moderator notification at `:883` does
  not.
- **`delete_post:782-788` duplicates `mod_remove_post`'s notification-removal
  block verbatim.** Group B registered it from the other side; this is the
  second sighting and the entry should say so.
- **The `user_id = 1` fallback is REACHABLE**, via
  `app/shared/tasks/maintenance.py:150` and `:185`, and `:760`'s
  `if current_user:` is correct rather than defective. Registered because an
  earlier draft of this spec concluded the opposite, and the next reader should
  not have to re-derive the refutation.
- Whatever the mutation pass finds, including every survivor with its argument.

## Risks carried deliberately

- **`report_post` is the largest single function this campaign has covered in
  one task.** 57 statements and 36 arcs, with four nested loops and conditionals
  and two external dependencies (`Site.admins()`, `force_locale`). The plan
  should split it across at least two tasks rather than one.
- **The `Site.admins()` dependency is not inert.** Every `notify_admins`
  assertion rests on it, and `s.author` being User id 1 means the seeded fixture
  may already contain an admin. Task 1 probes it; if the probe surprises,
  several later tasks change shape.
- **PC2's defect cannot be triggered by a natural fixture.** It needs a
  community id that collides with an instance id already in the set. The test
  must construct that collision deliberately, and say in its docstring that it
  is constructing an artificial coincidence rather than a realistic one.

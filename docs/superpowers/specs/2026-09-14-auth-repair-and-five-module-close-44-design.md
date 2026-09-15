# Coverage sub-project 44: the login ban bypass, and three modules closed

**Date:** 2026-09-14
**Branch:** `blentz`
**Measured at:** `83f4cd6d` (full suite, `PYTEST_EXIT=0`, 4932 passed, 3 skipped, all 22 floors met)

## Goal

Three parts, in this order.

**Part 1 — repair a live authentication bypass.** `app/shared/auth.py:57-75` refuses a
banned user once and then lets them in on every subsequent attempt.

**Part 2 — close three modules**: `app/shared/domain.py`, `app/shared/auth.py` and
`app/shared/upload.py`. Together 112 statements and 72 arcs. (This read "five modules" and
"116 statements" before the CORRECTION below withdrew Group A.)

**Part 3 — pay down the two highest-ranked survivors** sub-project 43 registered.

Part 1 comes first because it is a live security defect and Parts 2 and 3 are not.

## Part 1: the login ban bypass

### The defect, established at source

`app/shared/auth.py:57-75`:

```
57  if user.id != 1 and (user.banned or user_ip_banned() or user_cookie_banned()):
58      # Detect if a banned user tried to log in from a new IP address
59      if user.banned and not user_ip_banned():
60          # If so, ban their new IP address as well
61          new_ip_ban = IpBan(ip_address=ip_address(), notes=user.user_name + ' used new IP address')
...
66          if src == SRC_WEB:
67              flash(_('You have been banned.'), 'error')
...
73              return response
74          elif src == SRC_API:
75              raise Exception('incorrect_login')
77  if src == SRC_WEB:      <- falls through to here and logs the user in
```

**The refusal at `:73`/`:75` is nested inside `:59`'s new-IP detection.** The outer guard at
`:57` admits four ban states; only one of them is refused.

| `user.banned` | `user_ip_banned()` | `:57` | `:59` | outcome |
|---|---|---|---|---|
| True | False | enters | True | IP banned, **refused** — the only correct case |
| True | True | enters | False | **falls through, logs in** |
| False | True | enters | False | **falls through, logs in** |
| False | cookie only | enters | False | **falls through, logs in** |

The second row is the serious one, because the first row *creates* it. A banned user's first
attempt bans their IP and is refused; **every subsequent attempt from that same IP satisfies
row two and returns a JWT.** The ban is self-defeating after one try.

`user_ip_banned()` is `app/utils.py:2311-2314`; `user_cookie_banned()` is `:2405-2407`.
The sole caller is `app/api/alpha/routes.py:1277`, which adds `enable_api()` and a
`20/hour` rate limit and **no compensating ban check**.

### The fix

Dedent `:66-75` one level, out of `:59`'s block and into `:57`'s, so the refusal applies to
every state the outer guard admits. `:59-64` keeps its own body — banning the new IP is
correct and stays where it is.

### What Part 1 must prove

The failing observation comes first, as in sub-projects 40 through 43: **all four rows of the
table above pinned against today's behaviour**, three of them asserting that a login
succeeds, before any production line changes. Then the dedent, then the three pins inverted.
Row one must keep passing unchanged throughout — it is the control that proves the dedent did
not simply disable the branch.

## Part 2: the modules

### Measurement, from the full-suite JSON at `83f4cd6d`

| Module | percent | missing stmts | missing arcs | floor |
|---|---|---|---|---|
| `app/shared/tasks/notes.py` | 99.087 | 2 | 0 | 99 |
| `app/shared/tasks/pages.py` | 99.461 | 2 | 0 | 99 |
| `app/shared/domain.py` | 14.000 | 27 | 16 | none |
| `app/shared/auth.py` | 43.548 | 40 | 30 | none |
| `app/shared/upload.py` | 44.531 | 45 | 26 | none |
| | | **112 in scope** | **72** | |

(The `notes.py` and `pages.py` rows are left in place as the measurement that prompted
Group A. Their 4 statements are NOT in this round's scope — see the CORRECTION above.)

### CORRECTION, added after this spec was committed: Group A is WITHDRAWN

**Group A as written below is wrong, and the two modules it would have closed stay
open.** The correction is kept in place rather than deleted, because what it got wrong
matters more than what it proposed.

This spec claimed `notes.py:100-101` and `pages.py:107-108` were an oversight that a
monkeypatch would close. They are not an oversight. **Sub-projects 19 and 20 reached
them, analysed them, and ruled them unreachable deliberately**, filing them under this
campaign's own taxonomy as `tests/README.md` fact 75, **cause 4(c)** — a handler for an
exception the callee cannot raise on this path. The argument is written out at
`tests/test_shared_tasks_send_reply.py:1565-1588`:

> `search_for_user` cannot raise for a bare local name. `:95` has already established
> that the mention's host half equals `SERVER_NAME`, so `user_name` reaching `:99`
> carries no `'@'` and no scheme, and `search_for_user` takes its local branch — a query
> returning None for a miss, not an exception. The remote half of the same scan
> (`:102` onward) is where a raise is possible, and it has its own handler.

That is sound, and this spec's author had not read it before proposing the monkeypatch.

**Why this is NOT the shape sub-project 43 retracted.** D564's error was ruling two lines
equivalent when a legitimate third `src` value reached them and the twin module already
shipped the test — a real input, already precedented. Here there is no input at all: the
handler can only be entered by making the callee do something it provably cannot do.
Covering it with a double would assert a counterfactual and would overturn two deliberate
prior rulings on this spec's say-so rather than on evidence they were wrong.

**The distinction, stated so the next round does not have to re-derive it:** "no
production caller reaches it" is not a reason to stop measuring, and that was D564's
mistake. "No possible input reaches it, because the callee cannot produce the condition"
is a different claim, and it is the one cause 4(c) names. Ask which of the two you have.

**Consequence for this round.** `app/shared/tasks/notes.py` and `app/shared/tasks/pages.py`
stay at floor 99. Part 2 closes **three** modules, not five: `domain.py`, `auth.py` and
`upload.py` — **112 statements and 72 arcs**. Every figure below that says five modules or
116 statements is superseded by this paragraph. The floors total is unchanged at 25,
because the three new entries were always the three this round adds.

**Left open for a later round, and worth settling once:** whether cause 4(c) is a
do-not-cover category or a cover-with-a-double category. The campaign has met this shape at
least twice (`send_post`'s `:107-108`, `send_reply`'s `:100-101`) and re-litigates it each
time. That question is bigger than one round.

### Group A — the bare-`except` twins (4 statements, two modules closed)

`app/shared/tasks/notes.py:100-101` and `app/shared/tasks/pages.py:107-108` are the same
construct:

```python
try:
    recipient = search_for_user(user_name)
except:
    pass
```

**Neither is reachable through `search_for_user`'s own behaviour.** The call site passes a
bare local `user_name` with no `@`, so `app/user/utils.py:92` sets `server = ''`, `:94`'s
branch is skipped — and with it the only `raise` in the function, at `:98` — and the function
returns a `User` at `:104` or `None` at `:109`.

**They are reachable by a test.** Both modules bind the name at module level
(`notes.py:7`, `pages.py:9`), so rebinding `app.shared.tasks.notes.search_for_user` to a
callable that raises reaches the handler. Sub-project 42 closed `app/shared/reply.py`'s last
line this way rather than with a pragma, and `.coveragerc:7` excludes only `pragma: no cover`
in any case.

**This distinction is the round's governing lesson and it is written here deliberately.**
Sub-project 43 ruled two lines equivalent on a proof that they were unreachable *from
production callers*, and the final review overturned it: a line no input can reach is not a
line no test can reach. See D564 and `tests/README.md` fact 241.

**Register, do not fix:** a bare `except:` catches `KeyboardInterrupt` and `SystemExit`. Both
sites, and `app/shared/auth.py:90`'s `except Exception: ...` around `sync_user_to_ldap` is the
milder relative.

### Group B — `app/shared/domain.py` (27 statements, 16 arcs)

`block_domain:9-29` and `unblock_domain:32-51` are structural twins of `block_another_user`
and `unblock_another_user`, which sub-project 43 closed. **The harness in
`tests/test_shared_user_blocks.py` transfers directly**: the `src` fork, `bearer`, `web_ctx`,
the id-1 burn.

They differ from their user-side twins in ways worth asserting rather than assuming: there is
**no self-block guard and no admin/staff guard**, and an unknown domain is a **silent
no-op** — `:17`/`:40`'s `if domain_to_block:` is false, nothing is written, and the API arm
still returns `user_id` as though it had worked. Register the silent no-op; do not fix it.

### Group C — `app/shared/auth.py` (40 statements, 30 arcs), carrying Part 1

**The entire `SRC_WEB` branch is dead code.** There are two functions named `log_user_in`:
`app/auth/util.py:474` takes `(user, form, ip, country, ldap_sync=True)` and serves the real
web login flow, and `app/shared/auth.py:18` takes `(input, src)` and is reached only from
`app/api/alpha/routes.py:1277` with `SRC_API`. The module's own comment at `:17` says so.

That makes `:21-24`, `:41-44`, `:47-53`, `:66-73`, `:77-81` and `:93-110` reachable **only by
a test passing `SRC_WEB` directly**. That is legitimate and precedented — see Group A's
lesson and `tests/test_shared_post_interactions.py:577` — but every such test must say in its
docstring that it drives a source value production never passes, and why.

Also register, do not fix: `:57`'s `user.id != 1` exempts the id-1 account from every ban
check, the production face of the id-1 trap this campaign keeps meeting in fixtures; and the
web arm looks up by exact `user_name` while the API arm additionally falls back to email
(`:29-33`), so the two arms accept different credentials.

### Group D — `app/shared/upload.py` (45 statements, 26 arcs)

The expensive group. `process_upload:16-123` needs **real image files** — Pillow opens and
re-encodes at `:68-82` — and its S3 branch at `:89-109` needs `store_files_in_s3()` true;
`tests/conftest.py:10` already imports `mock_aws` and `:524` already uses it.
`process_file_delete:126-134` is small, with a false arm at `:127` and another at `:130`.

**`:120-121`'s `if not url: raise Exception('unable to process upload')` looks unreachable**:
`url` is assigned at `:86` from an f-string that always contains `SERVER_URL` and a path, and
reassigned at `:106` from another. **Ask Group A's question before ruling on it**, and record
the answer either way: if no test can reach it without a production change, say so with the
proof and leave the line uncovered rather than inventing a reason to skip it.

## Part 3: two survivor recipes

Sub-project 43's mutation pass left 46 survivors, registered as D565-D574 with recipes. This
round takes the two it ranked highest, and only those two.

1. **`app/shared/user.py:201` — `ban_ip_address` is not pinned (M68).** No test covers
   `(ban_ip_address=False, address present)`, so the flag can be dropped from `:201`'s `and`
   and the suite stays green while everyone banned is IP-banned. One test cloned from
   `test_ban_user_creates_an_ip_ban`.
2. **`app/shared/user.py:175-176` — the remote purge need not purge (M53, M54).** The branch
   is selected correctly and its bookkeeping is asserted, but `delete_dependencies()` and
   `purge_content(flush=flush_cdn)` can both be deleted with the suite green.

The other 44 stay registered. **`app/shared/user.py` is at 100 and must still measure 100
afterwards** — these tests add evidence, not coverage.

## Verification

A mutation pass **scoped by the statement list, not the arc table**, with both the statement
and compound lists derived mechanically via `ast.walk` and **the derivation command and its
raw output published beside every count**.

**Choose the oracle by measuring it, not by naming it.** Twice in sub-project 43 an agent
caught a controller dispatch prescribing test files that did not execute the function under
test — the three `test_shared_user_*.py` files do not touch `subscribe_user` at all, and a
later four-file set still under-reported seven lines whose coverage comes from
`tests/test_redirect_targets.py` and `tests/test_instance_util.py`. **Before the pass runs,
measure the candidate oracle and confirm it reproduces the full-suite figure for the module.**
An oracle that does not execute a function turns every mutant there into a survivor.

**Two mandatory mutations beyond the derived list:**

1. **Re-nest the fixed refusal** — put `:66-75` back inside `:59`. The three inverted pins
   from Part 1 must fail. If they do not, the fix is decorative.
2. **Neutralise `:57`'s `user.id != 1`** to `True`. Nothing should die, because no test should
   depend on the id-1 exemption; if something does, that test is asserting the exemption by
   accident and needs saying so.

Standing rules: a crash kill is not a kill unless a viable non-crashing variant also dies;
fix-catching is not a unique kill; an operator can be structurally void; **an equivalence
claim needs a proof of unkillability, never a failure to kill**; **a count is a claim**.

## Production changes

**One: Part 1's dedent of `app/shared/auth.py:66-75`.** Nothing else. Every other defect this
spec names is registered rather than fixed. Sub-projects 40 through 43 each made production
changes and each named them in the spec before the round began; this one names exactly one.

## Success criteria

- The four ban states pinned, the dedent made, three pins inverted, row one still passing.
- All three modules at `missing_lines []` and `missing_branches []`, **checked as lists**, with
  any line ruled unreachable carrying a proof that no *test* can reach it — not merely that no
  production caller does.
- Floors: **new** entries for `domain.py`, `auth.py` and `upload.py` at their measured
  values. 25 floors total. `notes.py` and `pages.py` stay at 99 — see the CORRECTION.
- `app/shared/user.py` still at 100 after Part 3.
- Full suite green, run by the controller, floors check chained with `&&` and **both**
  arguments, against a `--cov=app` JSON — a narrow `--cov` makes
  `tests/check_coverage_floors.py` report absent modules as 0.0 and fail falsely.
- Findings registered from **D575**; `tests/README.md` facts from **251**.

## Out of scope, carried forward

- **The other 44 survivors from sub-project 43** (D565-D574), each with its recipe.
- **D543's code half** — five unguarded arithmetic sites on `post.reply_count_cross_posted`;
  the backfill repaired the data only.
- **The D545 citation sweep**, D553, D443's reply twin, the `notify_admins` policy divergence,
  and the roughly twenty older open entries.
- **`app/shared/community.py` (410/193) and `app/shared/feed.py` (334/153)** are the last two
  large `app/shared` modules and each needs decomposing into groups before it can be scoped,
  exactly as `post.py`, `reply.py` and `user.py` were.

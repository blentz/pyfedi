# Sub-project 24: closing `app/shared/tasks/flags.py` and `app/shared/tasks/users.py`

Design document. Branch `blentz`. Follows sub-project 23, which closed
`app/shared/tasks/adds.py` and `app/shared/tasks/removes.py` to 100.0000% each
(16 commits, `79ea4e1e..896150f1`).

## 1. Why these two modules, together

This is the first sub-project to take two modules that are **not** twins. The
user chose the pairing over a single-module scope after being shown the
trade-off, and the reason it is coherent rather than merely two jobs is that
each module answers a different question the campaign has been deferring:

- `flags.py` is the **sixth** instance of a shape already closed five times
  (`notes.py`, `pages.py`, `adds.py`, `removes.py`). It tests whether the
  harness built for that shape now transfers without rediscovery. If it does,
  the shape is closed as a *class* and not merely at five sites.
- `users.py` is the first target that is **not federation-shaped at all**. It
  carries a confirmed, never-worked production bug, and closing it requires a
  harness the campaign does not yet own: HTTP doubles, a controlled `random`,
  and a suppressed `sleep`.

Taking them together buys the comparison. Taking either alone does not.

## 2. Baselines

Measured from `scratch_full_cov.json` at `896150f1`. Re-derive before starting;
do not trust these numbers if the report's mtime predates HEAD.

| Module | Statements | Branches | Partial | Missing | Percent |
|---|---|---|---|---|---|
| `app/shared/tasks/flags.py` | 42 | 6 | 0 | 31 | 22.9167% |
| `app/shared/tasks/users.py` | 58 | 20 | 0 | 48 | 12.8205% |

`flags.py` missing lines: 26-34, 36, 41-49, 51, 55-58, 60-62, 73-76.
`users.py` missing lines: 15-19, 21, 23-25, 27, 29-32, 34-37, 43-44, 46-48, 50,
53-58, 60-63, 69-70, 72-73, 75-77, 79-80, 82-85, 87.

Target: zero missing statements and zero partial branches in both, or a residual
proved unreachable and documented with the proof.

Coverage must be collected with the **dotted module form**
(`--cov=app.shared.tasks.flags`). A path form (`--cov=app/shared/tasks/flags.py`)
collects nothing, writes no JSON, and exits 0 — it fails silently and green.

## 3. `flags.py` — the shape, sixth instance

78 lines, three functions:

```
:24  @celery.task
:25  def report_reply(send_async, user_id, reply_id, summary, instance_ids)
:30      reply = session.query(PostReply).filter_by(id=reply_id).one()
:31      report_object(session, user_id, reply, summary, instance_ids)
:32-36   except Exception: session.rollback(); raise   / finally: session.close()

:39  @celery.task
:40  def report_post(send_async, user_id, post_id, summary, instance_ids)
:45      post = session.query(Post).get(post_id)
:46      report_object(session, user_id, post, summary, instance_ids)
:47-51   except Exception: session.rollback(); raise   / finally: session.close()

:54  def report_object(session, user_id, object, summary, instance_ids)
:56      community = object.community
:57      if community.local_only or not community.instance.online(): return
:60-71   the Flag envelope
:73      instances = session.query(Instance).filter(Instance.id.in_(instance_ids))
:74-76   for instance in instances: if instance.inbox is not None: send_post_request(...)
```

### 3.1 What transfers from the twins

Both wrappers use `patch_db_session` (`:29`, `:44`), unlike sub-project 21's
`send_answer`. Both have the identical `except`/`finally` tail. The
`_recording_task_session` helper — which wraps a **genuine** `Session` and
records `rollback`/`close` so the recorded **order** is the assertion — applies
directly. It currently exists in two copies
(`tests/test_shared_tasks_send_answer.py:567`,
`tests/test_shared_tasks_send_reply.py:1637`); a third copy is acceptable, and
the duplication is a finding to record, not to fix here.

`get_task_session` must be patched in **the target module's own namespace**
(`app.shared.tasks.flags`), never globally.

### 3.2 The lookup asymmetry, again

`:30` uses `.filter_by(id=reply_id).one()`, which raises `NoResultFound` for a
missing reply. `:45` uses `.get(post_id)`, which returns `None`, so `:56`
`object.community` raises `AttributeError`. **Two different failure modes,
fifteen lines apart, in one file** — the same shape found in `notes.py`
(`send_reply:81` vs `send_answer:246`), in `pages.py`, and in both twins
(`:32` vs `:47`).

Both must be asserted, by a **natural raise** — construct the missing-row
condition, do not inject an exception.

This asymmetry recurs in every module the campaign has opened. Before allocating
a new register number for it, **read the register and find out whether D316 or
D317 already carries it**, and extend that cell in place if so. The campaign's
own copy-hunt rule applies to register cells: enumerate, or the next reader
invents a number.

### 3.3 The gate at `:57` — D309's sixth site

`if community.local_only or not community.instance.online():` omits
`community.private`. D309's cell currently counts **six** omitting sites across
**seven lines** — `blocks.py:104`, `deletes.py:127` **and** `:130`,
`flags.py:57`, `groups.py:59`, `likes.py:60`, `locks.py:89`. The count is by
**file**, not by line: `deletes.py` contributes two guards and counts once.
State which unit you are using in any number you write, because six and seven
are both correct answers to differently-posed questions, and the register's
"eight to six" was posed the first way.

**Verify both the unit and the list against the register before writing a
number**, since sub-project 23 already moved this cell once.

Closing `flags.py:57` drops it to five files, six lines. This is production
change 1.

### 3.4 `:73` bypasses `following_instances()` — a new finding

Every other sender in `app/shared/tasks/` iterates
`community.following_instances()` (`app/models.py:842-851`), which filters
`Instance.dormant == False` at `:849` and `Instance.gone_forever == False` at
`:850`. `report_object` does not. It runs a raw
`session.query(Instance).filter(Instance.id.in_(instance_ids))` and guards only
on `instance.inbox is not None`.

Consequence: a report is delivered to a dormant or permanently-gone instance,
which no other outbound path in this package does. Record it at its **measured**
strength: this is a mechanism, and whether it produces a user-visible failure
depends on `send_post_request`'s behaviour against a dead host, which this
sub-project does not investigate. Do not promote it to a crash it has not been
shown to cause.

This is the mirror image of D302. D302 is the *redundant* `online()` check
layered on top of a filter that already excludes those instances; this is the
filter's **absence** where the redundant check would have been harmless. Note
the relationship in the register entry — they are the same fact read from
opposite ends.

Not fixed in this sub-project. Registered.

### 3.5 The envelope, and what does *not* apply

Assert on **serialized outbound request bytes** via the `http_mock` respx
router, never on an in-memory dict — the code mutates dicts after delivery.

`@context` is at `:67`, inside the `flag` object. Here `flag` **is** the
top-level posted object; there is no Announce wrapper. The nested-`@context`
absence assertions that carried sub-projects 20-23 **do not apply to this
module**, and asserting their absence here would be asserting a property of a
structure that does not exist. `app/activitypub/signature.py:100-101` reinjects
`@context` top-level only, which is consistent with, and invisible against,
what `:67` already sets.

### 3.6 Ordering — do not reintroduce the flake

`:73` returns rows through the **task** session, so the `Instance` objects the
loop touches are not the test's objects, and `.in_()` imposes no order. Every
assertion over delivered inboxes must be **set-based or sorted**, never an
ordered list. Ruling 4 of sub-project 23 existed to remove the last two ordered
assertions from the suite; this module must not add a third.

Per fact 132, any monkeypatch of a model attribute reached through the task
session must be **class-level** (`monkeypatch.setattr(type(obj), ...)`).
Instance-level patching is correct only where the test's own object is passed
straight through — which is true of `report_object`'s `object` and `session`
parameters when it is called directly, and false when it is reached through a
wrapper. Decide per call site by reading the path, and confirm with a raising
probe. Do not copy the form from a neighbouring file.

## 4. `users.py` — the module that is not federation-shaped

87 lines, one function, `check_user_application(application_id, send_async=True)`
at `:14`. Structure:

```
:15   session = get_task_session()          # no patch_db_session anywhere (D314)
:17   application = session.query(UserRegistration).get(application_id)
:18   if not application or not application.user: return
:23   for domain in get_setting('ban_check_servers', '').split('\n'):
:24-25    if not domain.strip(): continue
:27       try:
:29-32        three fake IPs from random.randint octets
:34           ip_index = random.randint(0, len(fake_ips))
:35-36        ip_list = fake_ips[:]; insert the real ip_address at ip_index
:37-41        ip_response = httpx_client.post(.../api/is_ip_banned, timeout=5)
:43-47        if 200: json; if results and len > ip_index and results[ip_index]: num_banned += 1
:48           ip_response.close()
:50           sleep(random.randint(1, 30))
:53-58        three fake emails
:60-62        email_index; email_list; insert the real email
:63-67        email_response = httpx_client.post(.../api/is_email_banned, timeout=5)
:69-73        same four-way check; num_banned += 1
              (email_response is NEVER closed)
:75-77    except Exception as e: log; continue
:79-82  if num_banned > 0: session.execute(text(SQL, params)); session.commit()
:83-87  except Exception: session.rollback(); raise / finally: session.close()
```

### 4.1 D319 — the `text()` call has never worked

`:80-81` reads:

```python
session.execute(text('UPDATE "user_registration" SET warning = :warning WHERE id = :id',
                        {'warning': f"{num_banned} instances have banned this account.", 'id': application_id}))
```

The params dict is a **second positional argument to `text()`**, not the second
argument to `session.execute()`. Count the parentheses: `text(` closes after the
dict, and `session.execute(` receives one argument.

`sqlalchemy.text` takes exactly one positional parameter. Measured in the test
container at SQLAlchemy 2.0.52:

```
text sig: (text: 'str') -> 'TextClause'
TypeError: text() takes 1 positional argument but 2 were given
```

So **every** run reaching `num_banned > 0` raises `TypeError`, which `:83`
catches, rolls back and re-raises. The `warning` column is never written. The
ban-check feature has never worked in this shape, and coverage at 12.82% is why
nobody noticed.

Fix (production change 2):

```python
session.execute(text('UPDATE "user_registration" SET warning = :warning WHERE id = :id'),
                {'warning': f"{num_banned} instances have banned this account.", 'id': application_id})
```

**The test must assert the persisted `warning` column**, read back after the
call. Asserting that `session.execute` was called would be satisfied by the
broken code the moment the line is reached, and is precisely the unfailable
shape fact 132 warns about. `UserRegistration.warning` is `String(100)`
(`app/models.py:3640`); the message fits.

### 4.2 `email_response` is never closed

`ip_response.close()` at `:48` has no counterpart after `:73`. Production
change 3 adds one, mirroring `:48`.

**This is the assertion most at risk of being vacuous**, and the harness is
designed around that risk. `respx` cannot observe it: the `Response` object is
local to the function, never returned, and a respx-mocked response may already
report `is_closed == True` before `close()` is called — an assertion on
`is_closed` would pass before and after the fix.

Therefore `httpx_client` is replaced in the module namespace by a **recording
double**: its `post()` returns a scripted response stub whose `close()` appends
to a list. The assertion is that **both** responses appear in that list. That
fails before the fix (one entry) and passes after (two). Verify the failure
before writing the fix.

### 4.3 The three harness controls

All three patch names in `app.shared.tasks.users`, the module's own namespace.

1. **`sleep`** — imported by value at `:1` (`from time import sleep`), so the
   module holds its own reference. Patch to a no-op. Without this the suite
   gains up to 30 seconds **per domain**, which alone would blow the session
   timeout.
2. **`random.randint`** — stub returning its lower bound. That makes
   `ip_index = 0`, `email_index = 0`, the IP octets fixed, and `sleep(1)`.
   Deterministic and readable. Note `random` is imported as a module (`:5`), so
   patching `randint` on it affects the shared module for the test's duration;
   `monkeypatch` reverts it, which is acceptable.
3. **`httpx_client`** — the recording double described in 4.2. It replaces
   respx for this module. Do not mix the two here: the double must be the only
   transport, or the close-log becomes ambiguous.

### 4.4 `get_setting` is not a caching trap

`get_setting` is decorated `@cache.memoize(timeout=500)` (`app/utils.py:202`),
which invites a design defending against cross-test cache leakage. **That
defence is unnecessary.** `.env.test` sets `CACHE_TYPE=NullCache`, so the
memoization is inert under test and `get_setting` queries the database on every
call. Use `set_setting('ban_check_servers', ...)` (`app/utils.py:215-222`),
which also calls `cache.delete_memoized`. Record this as a harness fact so no
future round designs around a cache that is not there.

### 4.5 Two sessions in one function — D314's carrier

`users.py` never calls `patch_db_session`. `application` is read through
`get_task_session()` (`:15`, `:17`), while `get_setting` reads through the
global `db.session` (`app/utils.py:204`). Both are live in the same function.

Tests must not assume a single session: a `Settings` row written through the
test's `db.session` is visible to `get_setting`, and the `UserRegistration` row
must be visible to the **task** session, which means committed before the task
runs. The `warning` read-back after the fix must come from a session that can
see the task session's commit.

Not fixed here — wrapping the body in `patch_db_session` is a behavioural change
that redirects every `db.session` read in the call tree, and its blast radius
exceeds this sub-project. Registered as D314's carrier, with the mechanism
measured and no consequence claimed beyond it.

### 4.6 Branch inventory

The 20 branches are reachable as follows. Each needs both arms.

- `:18` — no application; application with `user is None`; both present.
- `:23` — empty setting (loop body never runs); one domain; two domains.
- `:24` — a blank/whitespace line reaching `continue` at `:25`.
- `:43` — `status_code == 200` and not.
- `:46` — three ways to fail (`ip_results` falsy, `len <= ip_index`,
  `results[ip_index]` falsy) and the success.
- `:69`, `:72` — the same four for the email leg.
- `:75` — an exception raised inside the domain body, proving the loop
  **continues** to the next domain rather than aborting. Assert the second
  domain was still requested.
- `:79` — `num_banned > 0` and `== 0`.
- `:83` — the outer handler, via `_recording_task_session`, asserting the
  recorded rollback-then-close order.

## 5. Production changes — three, one line each

| # | Site | Change | Register |
|---|---|---|---|
| 1 | `flags.py:57` | add the `community.private or` conjunct | D309 site count 6 → 5 |
| 2 | `users.py:80-81` | move the params dict out of `text()` into `session.execute()` | **D319** |
| 3 | `users.py` email leg | add `email_response.close()` mirroring `:48` | **D320** |

Each is test-first: write the test, watch it fail for the **stated** reason
(not merely fail), then make the change. Change 2's pre-fix failure must be the
real `TypeError: text() takes 1 positional argument but 2 were given`.

`app/`'s numstat for the whole sub-project should be small and must be stated
exactly in the final report. Both files' line counts change (change 3 adds a
line); assert the expected `wc -l` after every mutation restore, alongside an
empty `git diff -- app/`.

## 6. Findings to register

Next free number is **D319** — confirmed against the register at `896150f1`; a
`D322` sighting in `docs/` is base64 inside an SVG, not an allocation. Facts end
at **132**; append from **133**.

1. **D319** — `users.py:80-81`, the `text()` params bug. A production defect
   that has never worked, found by coverage rather than by a report.
2. **D320** — `users.py`, `email_response` never closed against `:48`'s
   `ip_response.close()`.
3. **D321** — `flags.py:73` bypasses `following_instances()`, so it lacks the
   `dormant`/`gone_forever` filter every other sender inherits. D302 read from
   the opposite end; say so in the cell.
4. **D314's `users.py` carrier** — extend D314's cell in place; do not allocate.
5. **The lookup asymmetry at `:30`/`:45`** — extend D316 or D317 in place if
   either already carries the shape; read the register first.
6. **`_recording_task_session` now exists in three copies** — a test-side
   duplication finding, recorded not fixed.

New harness facts:

- **133** — `CACHE_TYPE=NullCache` in `.env.test` makes every `@cache.memoize`
  inert under test. Do not design defences against stale memoized values.
- **134** — `respx` cannot observe `Response.close()`, because the response
  never leaves the function under test. Proving a close requires replacing the
  client with a recording double. An `is_closed` assertion may be unfailable.
- **135** — `sleep` imported by value (`from time import sleep`) must be patched
  in the importing module's namespace. `users.py:50` sleeps up to 30 seconds per
  domain and would otherwise dominate the suite.

## 7. Verification

- **Mutations**, per the established instrument: one at a time, targeted
  single-line `sed`, **dry-run without `-i` and read the produced line before
  applying**, restore with `git checkout -- app/`, then assert both an empty
  `git diff -- app/` and the expected `wc -l`. Every mutation must be killed by
  a named test, and the kill must be recorded.
- **Floors**: add `app/shared/tasks/flags.py` and `app/shared/tasks/users.py`
  entries at whatever is achieved. Floors only rise.
- **Floor bite** must be proved, not assumed. At 100% there is no headroom to
  raise a scratch floor, so use sub-project 23's inversion: hold the floors
  fixed, regress the report, and include an isolation control.
- **Full suite** in the foreground, **unpiped**. `pytest` exits 1 on a session
  timeout and `run_tests.sh` propagates it; a pipeline eats the status, so read
  `${PIPESTATUS[0]}` or do not pipe.
- Test counts come from **collection**, never from `grep -c '^def test_'`.
- Before believing any failure, run `./run_tests.sh --down` — a wedged podman
  stack reports failures that are not regressions.
- Every line number in every document re-derived against the current tree, and
  every HEAD-relative number anchored to the commit it was true at.

## 8. Risks

1. **The `email_response` assertion is the one most likely to be born
   unfailable.** Section 4.2's double exists for that reason. Confirm the
   pre-fix failure with a raising or counting probe before trusting the test.
2. **Two harnesses in one sub-project doubles the review surface.** The
   `flags.py` half should be finished and reviewed before the `users.py` half
   begins, so a defect in the new harness cannot be mistaken for a defect in the
   transferred one.
3. **`users.py`'s two live sessions** (4.5) make row visibility the likeliest
   source of confusing failures. Commit fixture rows before invoking the task.
4. **`flags.py:73`'s task-session objects** make instance-level patching wrong
   in the wrapper paths and right in the direct-call paths, within one file.
   Fact 132's decision question applies per call site: does the production path
   re-load the object, or is the test's object passed straight through?

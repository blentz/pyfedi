# Sub-project 44: the login ban bypass, and three modules closed

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair a live authentication bypass that lets a banned user log in on every attempt after their first, then close `app/shared/domain.py`, `app/shared/auth.py` and `app/shared/upload.py` — 112 statements and 72 arcs.

**Architecture:** Part 1 pins all four ban states against today's behaviour — three of them asserting that a login currently succeeds — then dedents the refusal one level and inverts those three. Part 2 closes three modules, two of which are already partly covered by test files not named after them. Part 3 takes the two highest-ranked survivors sub-project 43 registered.

**Tech Stack:** Python 3.13, Flask, SQLAlchemy 2.0.52, Pillow, boto3 + moto, pytest, podman-compose. No host Python has flask or pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-auth-repair-and-five-module-close-44-design.md` (corrected at `f7326a47`; **read its CORRECTION subsection first** — it withdraws the Group A this plan therefore does not contain).

---

## Global Constraints

Every task's requirements implicitly include this section. These are not style preferences. Each line is here because it cost this campaign a defect, a retraction, or a day.

### The rule this round exists to honour

**A line no *input* can reach is not always a line no *test* can reach — but sometimes it is, and the two cases are different.** Sub-project 43 ruled two lines equivalent because no production caller reached them, when a legitimate third argument value did; that ruling was retracted (D564). Sub-projects 19 and 20 ruled two other lines unreachable because the callee cannot raise on that path; that ruling stands. **Before calling anything uncoverable, say which of the two you have, and prove it.** `tests/README.md` fact 75 catalogues the causes; cause 4(c) is the one that stands.

### Choosing a measurement oracle

**Measure the oracle; never name it.** Twice in sub-project 43 a dispatch prescribed test files that did not execute the function under test, turning covered lines into apparent gaps and every mutant into a survivor. For this round, verified by reading:

- **`app/shared/auth.py`'s `SRC_WEB` arm is already exercised by `tests/test_redirect_targets.py:234-265`** (`TestSharedAuthNextPageIsChecked`).
- **`app/shared/upload.py` is already exercised by `tests/test_utils_security.py`**, which imports `process_upload` at `:35` and drives it for real around `:993-1049`.

**Any coverage run for Groups C or D must include those two files**, or it will under-report. Confirm your oracle reproduces the full-suite figure for the module before trusting a gap it shows you.

### Patching: rebind on the module that uses the name

`app/shared/auth.py:14` is `from app.utils import ip_address, is_safe_redirect_target, user_ip_banned, user_cookie_banned, banned_ip_addresses`. That `from ... import` binds each name into `app.shared.auth`'s globals at import time, so **patching `app.utils.user_ip_banned` would NOT intercept** — rebind `app.shared.auth.user_ip_banned`. The same applies to `app.shared.tasks.notes.search_for_user`, `app.shared.user.task_selector`, and every other unqualified call in this codebase. `app/utils.py:2308` is `ip_address = get_ip_address`, an alias, so the same rule holds for it.

### Running anything

- **There is no host Python with flask or pytest.** Everything goes through `./run_tests.sh` (= `podman compose exec -T test-runner pytest "$@"`, `run_tests.sh:85`). There is no `--exec` flag.
- `compose.test.yaml:67` bind-mounts `./:/app:z`, so **repo files ARE shared with the container**. Only `/tmp` is not. Write repo files normally; inline container Python as `podman-compose -f compose.test.yaml exec -T test-runner python -c "..."`.
- `run_tests.sh:83` runs `flask db upgrade` before every invocation.
- **Only the controller runs the full suite**, one pytest session at a time. **Never kill a running pytest** — teardown will not run, the test database is left corrupt, and the next run reports ~87 failures with `psycopg2.errors.UniqueViolation` on `site_pkey` that look like a code failure. Recover with `./run_tests.sh --down`. One run this session took **687s** and was left to finish in the background rather than killed.
- The suite needs `-o session_timeout=1800` as a command-line override. **`pytest.ini` must NOT be edited.**
- **`pytest` exits 1 on session timeout** and a pipeline eats the status — read `${PIPESTATUS[0]}`, or do not pipe.

### Measuring

- **Coverage takes the DOTTED module form** (`--cov=app.shared.auth`). A path form collects nothing, writes no JSON and exits 0 — a silent green failure.
- Write coverage JSON outside the repo; it lands in the **container's** `/tmp`.
- **`tests/check_coverage_floors.py` takes TWO arguments**, fails closed (exit 2) on one, and **counts a floored module absent from the report as 0.0** (`violations()`). So the floors check must run against a **`--cov=app`** JSON — a narrow one reports every other floored module as a false violation.
- **Test counts come from pytest's own output.** **A count is a claim** — publish the derivation beside it.

### Editing

- **Delete nothing the task did not create.**
- **`git checkout -- app/` is BANNED** while the round holds an uncommitted production change. Reverse edits by hand and re-read the restored line.
- **`git diff --quiet -- app/` is THE tree check**, not `wc -l`.
- **Verify against the COMMIT OBJECT**, never the working tree: `git show HEAD:<path>` and commit-to-commit `git diff --numstat`, plus `git status --porcelain`. A `git commit --amend` without staging changes the message only, and every working-tree check passes while the commit contains nothing — this happened in sub-project 43.
- Re-derive every line number with `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. **Never an `awk` that assigns to a field** — it rebuilds `$0` and destroys leading whitespace.
- **`/usr/bin/grep`, not the interactive `grep`** (a `ugrep` wrapper that silently skips gitignored paths).
- **No duplicate test names:** `/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" FILE | sed 's/^ *//' | sort | uniq -d`.
- **No ordered assertions over rows a query planner returned.** Compare sets.
- **A production change mid-round REOPENS COVERAGE.** Task 4 changes production; Task 7 re-measures everything after it.

### Evidence

- **An equivalence claim needs a proof of unkillability, never a failure to kill.**
- **A crash kill is not a kill** unless a viable non-crashing variant of the same fault also dies.
- **Fix-catching is not a unique kill.**
- **The five false-witness mechanisms:** (a) asserting on state something else sets unconditionally; (b) a fixture coincidence; (c) emptiness with no same-mechanism positive control; (d) an input taking the same path under both arms; (e) two conditions exercised only in lockstep.

### Committing

- `git commit -F <file>`, never `-m`. Lowercase `type:` subject prefix, prose body.
- Trailers, last two lines, **a fixed campaign literal — NOT the model running the task**:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```

### Scope

- **Production changes this round: exactly one** — Task 4's dedent. Everything else that surfaces is **registered**, not fixed.
- Register from **D575**. `tests/README.md` facts from **251**.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `tests/test_shared_domain.py` | `block_domain`, `unblock_domain`. |
| `tests/test_shared_auth_login.py` | `log_user_in` — both source arms, and the four ban states Part 1 turns on. |
| `tests/test_shared_upload.py` | `process_upload`, `process_file_delete`. |

**Modified:**

| File | Change |
|---|---|
| `app/shared/auth.py:66-75` | Dedented one level. **Task 4 only.** |
| `tests/test_shared_user_bans.py` | Two survivor tests appended. **Task 6 only.** |
| `coverage_floors.ini` | Three new entries. **Task 7 only.** |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | Entries from D575. **Task 9 only.** |
| `tests/README.md` | Facts from 251. **Task 9 only.** |

Three new files, one per module, matching the campaign's convention. `auth.py`'s tests are split across two tasks but one file, because the ban states and the source arms interlock.

---

### Task 1: `app/shared/domain.py`

**Files:**
- Create: `tests/test_shared_domain.py`
- Read: `app/shared/domain.py` (the whole file, 51 lines)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing later tasks rely on.

**Target:** 27 statements, 16 arcs, at 14.000%.

**Why this one is first: the harness already exists.** `block_domain:9-29` and `unblock_domain:32-51` are structural twins of `block_another_user`/`unblock_another_user`, which sub-project 43 closed in `tests/test_shared_user_blocks.py`. The `src` fork, `bearer(user)`, `web_ctx(app, user)` and the id-1 burn all transfer unchanged. **Read that file before writing this one.**

**The shape, re-derived at source:**

```
:10/:33   if src == SRC_API:  user_id = authorise_api_user(auth)  else  current_user.id
:15/:38   domain_to_block = db.session.query(Domain).filter(Domain.name == domain).first()
:17/:40   if domain_to_block:
:18/:41       existing_block = ...filter(DomainBlock.domain_id == ..., DomainBlock.user_id == user_id).first()
:19           if not existing_block:   /  :42  if existing_block:
:20-24            add + commit + cache.delete_memoized(blocked_domains, user_id)
:43-46            delete + commit + cache.delete_memoized(blocked_domains, user_id)
:26-29/:48-51  if src == SRC_API: return user_id  else: return None
```

**Three differences from the user-side twins, all to be REGISTERED and not fixed:** there is no self-block guard; there is no admin/staff guard; and **an unknown domain is a silent no-op** — `:17`/`:40` is false, nothing is written, and the API arm still returns `user_id` as though the block had been created.

- [ ] **Step 1: Write the file**

```python
"""`block_domain` and `unblock_domain` (app/shared/domain.py:9-51).

Both were at 14.000% before this file: 27 statements and 16 arcs missing.

These are structural twins of block_another_user/unblock_another_user
(app/shared/user.py:20-87), which sub-project 43 closed in
tests/test_shared_user_blocks.py -- the src fork, bearer, web_ctx and the
id-1 burn all transfer. What does NOT transfer is the user-side guards:
there is no self-block check and no admin/staff check here, and an unknown
domain is a silent no-op that still returns user_id on the API arm. Those
three are registered findings, not defects this round fixes, and the tests
below pin them as they are.
"""
from types import SimpleNamespace

import pytest

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Domain, DomainBlock
from app.shared.domain import block_domain, unblock_domain
from tests.factories import bearer, make_instance, make_user, web_ctx


def _seed_blocker():
    """A local user who is not id 1, and a Domain row to block.

    The first user minted in any test is id 1 deterministically --
    tests/conftest.py:131-132 runs `SELECT setval(c.oid, 1, false)` over every
    sequence after each test -- and `User.is_admin` (app/models.py:1259-1261)
    returns True for id 1 regardless of roles. Nothing in THIS module reads
    is_admin, but the burn is kept so a later test added to this file cannot
    inherit the trap silently. tests/test_shared_reply_make.py:247 is the
    precedent.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    blocker = make_user(instance, 'blocker', local=True)
    domain = Domain(name='spam.example', banned=False)
    db.session.add(domain)
    db.session.commit()
    return SimpleNamespace(blocker=blocker, domain=domain)


def test_block_domain_api_creates_the_block_and_returns_the_blocker(app, db_session):
    s = _seed_blocker()

    returned = block_domain('spam.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).filter_by(
        domain_id=s.domain.id, user_id=s.blocker.id).count() == 1


def test_block_domain_web_creates_the_block_and_returns_none(app, db_session):
    """:29 returns None and leaves the flash to its caller.

    Both the return value and the row are asserted: a function that created
    nothing would also return None.
    """
    s = _seed_blocker()

    with web_ctx(app, s.blocker):
        returned = block_domain('spam.example', SRC_WEB)

    assert returned is None
    assert db.session.query(DomainBlock).filter_by(
        domain_id=s.domain.id, user_id=s.blocker.id).count() == 1


def test_block_domain_is_idempotent(app, db_session):
    """:19's `if not existing_block` -- the false arm.

    The second call must not add a second row and must still return the
    blocker's id from :27 rather than falling out early.
    """
    s = _seed_blocker()
    block_domain('spam.example', SRC_API, bearer(s.blocker))

    returned = block_domain('spam.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 1


def test_block_domain_silently_does_nothing_for_an_unknown_domain(app, db_session):
    """PINS A DEFECT. :17's false arm.

    No Domain row named 'nosuch.example' exists, so nothing is written -- and
    the API arm still returns user_id at :27, indistinguishable from a
    successful block. A caller cannot tell the two apart. Registered rather
    than fixed: this round's production budget is the auth dedent.
    """
    s = _seed_blocker()

    returned = block_domain('nosuch.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 0


def test_block_domain_blocks_only_for_the_calling_user(app, db_session):
    """:18's filter is on BOTH domain_id and user_id.

    A second user's block of the same domain is seeded so that a lookup
    narrowed to domain_id alone would find it, conclude a block already
    exists, and skip :20-24. With one block row that fault is invisible.
    """
    s = _seed_blocker()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=bystander.id))
    db.session.commit()

    block_domain('spam.example', SRC_API, bearer(s.blocker))
    db.session.expire_all()

    owners = {b.user_id for b in db.session.query(DomainBlock).all()}
    assert owners == {s.blocker.id, bystander.id}


def test_unblock_domain_api_removes_the_block(app, db_session):
    s = _seed_blocker()
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=s.blocker.id))
    db.session.commit()

    returned = unblock_domain('spam.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 0


def test_unblock_domain_web_removes_the_block_and_returns_none(app, db_session):
    s = _seed_blocker()
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=s.blocker.id))
    db.session.commit()

    with web_ctx(app, s.blocker):
        returned = unblock_domain('spam.example', SRC_WEB)

    assert returned is None
    assert db.session.query(DomainBlock).count() == 0


def test_unblock_domain_leaves_another_users_block_alone(app, db_session):
    """:41's filter is on BOTH columns, in the delete direction."""
    s = _seed_blocker()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=s.blocker.id))
    db.session.add(DomainBlock(domain_id=s.domain.id, user_id=bystander.id))
    db.session.commit()

    unblock_domain('spam.example', SRC_API, bearer(s.blocker))
    db.session.expire_all()

    owners = {b.user_id for b in db.session.query(DomainBlock).all()}
    assert owners == {bystander.id}


def test_unblock_domain_is_idempotent(app, db_session):
    """:42's `if existing_block` -- the false arm. No block exists, so the
    body is skipped and :49 still returns the caller's id."""
    s = _seed_blocker()

    returned = unblock_domain('spam.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 0


def test_unblock_domain_silently_does_nothing_for_an_unknown_domain(app, db_session):
    """PINS A DEFECT. :40's false arm, the unblock twin of :17's."""
    s = _seed_blocker()

    returned = unblock_domain('nosuch.example', SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(DomainBlock).count() == 0
```

- [ ] **Step 2: Run, and investigate any failure at source**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_domain.py -v
echo "PYTEST_EXIT=$?"
```

Anticipate one thing: `Domain` may have required columns beyond `name` and `banned`. If the insert fails, read the model and add the minimum, and say so in the report. **Do not weaken an assertion to make a test pass.**

- [ ] **Step 3: Measure the module to zero**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_domain.py --cov=app.shared.domain \
    --cov-report=json:/tmp/domain.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/domain.json'))['files']['app/shared/domain.py']
print('percent', d['summary']['percent_covered'])
print('missing_lines   ', d['missing_lines'])
print('missing_branches', d['missing_branches'])
"
```

Note the **dotted** `--cov=app.shared.domain`. Both lists must be `[]`. If anything remains, add the test before committing.

- [ ] **Step 4: Duplicate-name and tree checks**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_domain.py | sed 's/^ *//' | sort | uniq -d
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
```

- [ ] **Step 5: Commit**

```bash
cd /home/blentz/git/pyfedi
git add tests/test_shared_domain.py
git commit -F <message-file>
git status --porcelain
git show HEAD:tests/test_shared_domain.py | /usr/bin/grep -c "^def test_"
```

Subject: `test: cover block_domain and unblock_domain`. Body: the module was at 14%; what transfers from the user-side twins and what does not; and the three registered differences, naming the silent no-op explicitly.

---

### Task 2: `log_user_in`'s API arm, and the four ban states

**Files:**
- Create: `tests/test_shared_auth_login.py`
- Read: `app/shared/auth.py:18-113`, and `tests/test_redirect_targets.py:234-265`

**Interfaces:**
- Consumes: nothing.
- Produces: `_seed_login_user(name='loginuser', password='correct horse battery')`, `_ban_state(...)` and the `Form`/`Field` doubles, all used again by Tasks 3 and 4 in the same file.

**Target this task:** the `SRC_API` arm and the ban block at `:57-75`. Task 3 takes the `SRC_WEB` arm.

**The defect this task pins.** `app/shared/auth.py:57-75`:

```
57  if user.id != 1 and (user.banned or user_ip_banned() or user_cookie_banned()):
59      if user.banned and not user_ip_banned():
60-64       ... create an IpBan for the current address ...
66          if src == SRC_WEB:  ... return response
74          elif src == SRC_API: raise Exception('incorrect_login')
77  if src == SRC_WEB:      <- falls through to here and logs in
```

The refusal is nested inside `:59`'s **new-IP detection**. Four states enter `:57`; only one is refused:

| `user.banned` | `user_ip_banned()` | `:59` | today's outcome |
|---|---|---|---|
| True | False | True | IP banned, **refused** — correct |
| True | True | False | **logs in** |
| False | True | False | **logs in** |
| False | cookie only | False | **logs in** |

Row one *creates* row two: refusing the first attempt bans the IP, so the next attempt from that address falls through. **Three of these four are pinned here asserting that a login currently succeeds, and Task 4 inverts them.**

**How to control the states.** `app/shared/auth.py:14` imports `ip_address`, `user_ip_banned`, `user_cookie_banned` and `banned_ip_addresses` into the module's own globals, so **rebind them on `app.shared.auth`** — patching `app.utils` would not intercept. This avoids needing real `IpBan` rows or request cookies.

- [ ] **Step 1: Write the file**

```python
"""`log_user_in` (app/shared/auth.py:18-113).

The module was at 43.548% before this file: 40 statements and 30 arcs.

TWO FUNCTIONS SHARE THIS NAME. app/auth/util.py:474 is
`log_user_in(user, form, ip, country, ldap_sync=True)` and serves the real
web login flow. THIS one, app/shared/auth.py:18, is
`log_user_in(input, src)` and is reached from exactly one caller --
app/api/alpha/routes.py:1277, with SRC_API. Its own comment at :17 says so.
So every SRC_WEB test in this file drives a source value production never
passes to this function, and says so in its docstring. That is legitimate
and precedented (tests/test_redirect_targets.py:234-265 already does it,
and tests/test_shared_post_interactions.py:577 is the campaign's canonical
case), but it must never be left implicit.

THREE TESTS HERE PIN A DEFECT. :57 admits four ban states and only one is
refused, because the refusal at :73/:75 is nested inside :59's new-IP
detection rather than hanging off :57. The three that fall through assert
that a banned or IP-banned user LOGS IN. They are inverted by the task that
dedents :66-75. Each says PINS A DEFECT in its docstring.
"""
import contextlib
from types import SimpleNamespace

import pytest

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Instance, IpBan, User
from app.shared.auth import log_user_in
from tests.factories import make_instance, make_user


class Field:
    """One WTForms-ish field. :22-23 and :106 read `.data` off these."""

    def __init__(self, data):
        self.data = data


class Form:
    """The duck-typed form the SRC_WEB arm reads.

    Copied from tests/test_redirect_targets.py:240-248, which already drives
    this function's SRC_WEB arm -- the same three fields, read at :22, :23
    and :106.
    """

    def __init__(self, user_name, password, low_bandwidth_mode=False):
        self.user_name = Field(user_name)
        self.password = Field(password)
        self.low_bandwidth_mode = Field(low_bandwidth_mode)


def _seed_login_user(name='loginuser', password='correct horse battery'):
    """A local user with a real password hash, not id 1.

    NOT id 1 on purpose: :57 begins `if user.id != 1`, exempting the first
    account from every ban check, and tests/conftest.py:131-132 resets every
    sequence between tests so the first user minted is id 1 deterministically.
    Without the burn, every ban test here would pass :57 vacuously.

    make_user(None, ...) sets instance_id=1 unconditionally, so the local
    Instance row has to exist first -- the same arrangement as
    tests/test_redirect_targets.py:80-89.
    """
    if not Instance.query.get(1):
        make_instance('test.piefed.local', software='piefed')
    burn = make_user(None, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 exemption at :57 moved; re-derive this'

    user = make_user(None, name, local=True)
    user.set_password(password)
    db.session.commit()
    return SimpleNamespace(user=user, password=password)


@contextlib.contextmanager
def _ban_state(monkeypatch, ip_banned=False, cookie_banned=False, ip='203.0.113.7'):
    """Drive :57 and :59's two predicates directly.

    app/shared/auth.py:14 binds ip_address, user_ip_banned and
    user_cookie_banned into THIS module's globals, so rebinding them on
    app.shared.auth is what intercepts -- patching app.utils would not, and
    app/utils.py:2308's `ip_address = get_ip_address` is an alias, so the same
    rule holds for it.

    Driving the predicates rather than seeding IpBan rows keeps each test's
    ban state a single explicit statement, and keeps :64's
    cache.delete_memoized(banned_ip_addresses) from depending on real rows.
    """
    monkeypatch.setattr('app.shared.auth.ip_address', lambda *a, **k: ip)
    monkeypatch.setattr('app.shared.auth.user_ip_banned', lambda *a, **k: ip_banned)
    monkeypatch.setattr('app.shared.auth.user_cookie_banned', lambda *a, **k: cookie_banned)
    yield


def test_log_user_in_api_returns_a_jwt(app, db_session, monkeypatch):
    """The success path: :26-29's username lookup, then :111-113's token."""
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert set(result) == {'jwt'}
    assert result['jwt']


def test_log_user_in_api_matches_the_username_case_insensitively(app, db_session, monkeypatch):
    """:29's `func.lower(User.user_name) == func.lower(username)`.

    The stored name is lower-case, so an upper-case input distinguishes a
    case-insensitive comparison from a plain equality.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = log_user_in({'username': 'LOGINUSER',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_api_falls_back_to_the_email_address(app, db_session, monkeypatch):
    """:31-33's second lookup, reached only when :29 found nothing.

    make_user sets email to f'{name}@example.com'. The web arm has no such
    fallback -- :24 matches user_name exactly -- so the two arms accept
    different credentials. Registered, not fixed.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = log_user_in({'username': 'loginuser@example.com',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_api_refuses_an_unknown_account(app, db_session, monkeypatch):
    """:35-37 -- both lookups missed."""
    _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'nobody', 'password': 'whatever'}, SRC_API)


def test_log_user_in_api_refuses_a_wrong_password(app, db_session, monkeypatch):
    """:46's true arm into :54-55's raise."""
    _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'loginuser', 'password': 'wrong'}, SRC_API)


def test_log_user_in_refuses_an_unknown_source(app, db_session):
    """:38-39's else. SRC_PUB is neither SRC_WEB nor SRC_API, so the function
    returns None before touching the database.

    SRC_PUB is used purely as a third source value to reach :39. It is not how
    this function is called in production -- app/api/alpha/routes.py:1277 is
    the only caller and passes SRC_API.
    """
    from app.constants import SRC_PUB
    _seed_login_user()

    with app.test_request_context('/'):
        assert log_user_in({'username': 'loginuser', 'password': 'x'}, SRC_PUB) is None


def test_log_user_in_api_bans_the_ip_of_a_banned_user_and_refuses(app, db_session, monkeypatch):
    """ROW ONE of the four ban states, and the ONLY one refused today.

    banned=True, ip_banned=False: :57 enters, :59 is true, :61-63 writes an
    IpBan for the current address, and :75 raises.

    THIS TEST IS NOT INVERTED by the dedent -- it is the control that proves
    the dedent did not simply disable the branch. Assert both halves: the
    refusal AND the IpBan row, because a dedent that broke :59-64 would still
    raise.
    """
    s = _seed_login_user()
    s.user.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=False):
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'loginuser',
                             'password': s.password}, SRC_API)

    assert db.session.query(IpBan).filter_by(ip_address='203.0.113.7').count() == 1


def test_log_user_in_api_admits_a_banned_user_whose_ip_is_already_banned(app, db_session, monkeypatch):
    """PINS A DEFECT -- and this is the serious one.

    ROW TWO: banned=True, ip_banned=True. :57 enters, but :59 is
    `True and not True` = False, so the refusal at :75 -- nested inside :59 --
    never runs, and control falls through to :83 and :111-113. A JWT is
    returned to a banned user.

    This state is not hypothetical: it is what ROW ONE produces. The banned
    user's first attempt bans their IP and is refused; every attempt after
    that, from the same address, is this test.

    THIS ASSERTION IS INVERTED by the task that dedents :66-75.
    """
    s = _seed_login_user()
    s.user.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_api_admits_an_ip_banned_user_who_is_not_banned(app, db_session, monkeypatch):
    """PINS A DEFECT. ROW THREE: banned=False, ip_banned=True.

    :57 enters on the second disjunct, :59's first conjunct is false, and the
    refusal never runs.

    THIS ASSERTION IS INVERTED by the task that dedents :66-75.
    """
    s = _seed_login_user()
    assert s.user.banned is False

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_api_admits_a_cookie_banned_user(app, db_session, monkeypatch):
    """PINS A DEFECT. ROW FOUR: banned=False, cookie_banned=True.

    :57 enters on the third disjunct and :59 is false.

    THIS ASSERTION IS INVERTED by the task that dedents :66-75.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, cookie_banned=True):
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_exempts_the_id_1_account_from_every_ban_check(app, db_session, monkeypatch):
    """PINS A DEFECT, registered not fixed. :57 begins `user.id != 1`.

    The id-1 account logs in while banned AND ip-banned. This is the
    production face of the id-1 trap this campaign keeps meeting in fixtures
    (app/models.py:1259-1261 makes the same account an admin outright).

    NOT inverted by the dedent: the dedent moves the refusal, it does not
    touch :57's first conjunct. This test must keep passing unchanged, which
    is what makes it evidence about :57 rather than about :59.
    """
    if not Instance.query.get(1):
        make_instance('test.piefed.local', software='piefed')
    first = make_user(None, 'firstaccount', local=True)
    assert first.id == 1
    first.set_password('correct horse battery')
    first.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            result = log_user_in({'username': 'firstaccount',
                                  'password': 'correct horse battery'}, SRC_API)

    assert result['jwt']


def test_log_user_in_stamps_last_seen_and_ip(app, db_session, monkeypatch):
    """:83-86. `ip` comes from :19's ip_address() call, patched to a known
    value, so this distinguishes the stamp from a column default."""
    s = _seed_login_user()
    assert s.user.ip_address is None

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip='198.51.100.9'):
            log_user_in({'username': 'loginuser', 'password': s.password}, SRC_API)

    db.session.expire_all()
    stored = db.session.query(User).get(s.user.id)
    assert stored.ip_address == '198.51.100.9'
    assert stored.last_seen is not None


def test_log_user_in_survives_a_failing_ldap_sync(app, db_session, monkeypatch):
    """:88-91's `except Exception: ...`.

    sync_user_to_ldap is bound into this module's globals by :12, so rebinding
    it here is what intercepts. The login must still succeed, which is the
    whole point of the handler.
    """
    s = _seed_login_user()

    def _boom(*args, **kwargs):
        raise RuntimeError('ldap is down')

    monkeypatch.setattr('app.shared.auth.sync_user_to_ldap', _boom)

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert result['jwt']
```

- [ ] **Step 2: Run, and treat every failure as a finding**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_auth_login.py -v
echo "PYTEST_EXIT=$?"
```

**The four PINS A DEFECT tests must PASS.** They assert today's wrong behaviour. If one fails, the defect is not what this plan says it is — **stop and report that**, because Task 4's fix is built on it.

Two things to anticipate: `get_country` at `:20` may need a request context (one is pushed) or may reach `current_app.config['COUNTRY_SOURCE_HEADER']`; and `sync_user_to_ldap` may be absent from the module's namespace under a different name — read `app/shared/auth.py:12` and use what is there.

- [ ] **Step 3: Measure, with the right oracle**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_auth_login.py tests/test_redirect_targets.py \
    --cov=app.shared.auth --cov-report=json:/tmp/auth.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/auth.json'))['files']['app/shared/auth.py']
print('percent', d['summary']['percent_covered'])
print('missing_lines   ', d['missing_lines'])
print('missing_branches', d['missing_branches'])
"
```

**`tests/test_redirect_targets.py` is in that command deliberately** — it already covers part of the `SRC_WEB` arm, and omitting it would show you gaps that are not gaps. Expect the `SRC_WEB` arm to remain largely open; Task 3 closes it. Paste the lists into your report.

- [ ] **Step 4: Checks and commit**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" tests/test_shared_auth_login.py | sed 's/^ *//' | sort | uniq -d
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git add tests/test_shared_auth_login.py
git commit -F <message-file>
git status --porcelain
```

Subject: `test: cover log_user_in's API arm and pin the ban-state bypass`. Body: the two same-named functions; the four-state table with today's outcome for each; that three tests assert a banned user logs in and will be inverted; and that row one is the control.

---

### Task 3: `log_user_in`'s web arm

**Files:**
- Modify: `tests/test_shared_auth_login.py` (append)
- Read: `app/shared/auth.py:21-24, 41-44, 47-53, 66-73, 77-81, 93-110`

**Interfaces:**
- Consumes: `_seed_login_user`, `_ban_state`, `Form`, `Field` from Task 2.
- Produces: nothing.

**Target:** the `SRC_WEB` lines Task 2 left open.

**Every test here drives a source value production never passes to this function.** Say so in each docstring, with the reason: `app/auth/util.py:474` is a different `log_user_in` that serves the real web flow, and `app/shared/auth.py:18`'s only caller passes `SRC_API`. The arm is live code, so it is covered; the docstring is what stops a future reader mistaking these for tests of the real login page.

- [ ] **Step 1: Append the tests**

Add `from flask import session as flask_session` to the imports. Cover, one test each:

- **`:21-24`** — the web arm's own lookup, `filter_by(user_name=username, ap_id=None)`, driven with `Form('loginuser', ...)`. Assert the returned response is a redirect, not a JWT dict.
- **`:41-44`** — `user is None or user.deleted`. Two tests, because the `or` has two operands and a single test leaves one deletable: one with an unknown name, one with an existing but `deleted=True` user. Both assert `'No account exists with that user name.'` is in `flask_session['_flashes']` and the response redirects to the login page.
- **`:47-53`** — a wrong password on the web arm, in two tests: one where `user.password_hash is None` (the `:48-51` reset-password Markup message) and one where it is set (`:52-53`'s plain `'Invalid password'`). The two messages differ, so assert the exact text; asserting merely that *a* flash happened would let the branches swap undetected.
- **`:66-73`** — the web arm of the ban refusal, ROW ONE only (`banned=True, ip_banned=False`). Assert the `'You have been banned.'` flash **and** `response.headers` carrying the `sesion` cookie set at `:72`, since that cookie is the mechanism `user_cookie_banned` later reads. **Mark it a control, not a pin** — it is row one, which the dedent does not change.
- **`:77-81`** — `waiting_for_approval()` redirecting to `auth.please_wait`, and the non-waiting path reaching `login_user` and setting `session['ui_language']`. Two tests. `User.waiting_for_approval` (`app/models.py:1254-1256`) looks for a `UserRegistration` row with `status=0`; `tests/factories.py:1126`'s `make_user_registration(user, answer='why', status=0)` creates exactly that.
- **`:93-110`** — the redirect and cookie tail. `tests/test_redirect_targets.py:250-265` already covers the `next`-parameter branches, so **do not duplicate them**; cover what it does not: `:106-109`'s `low_bandwidth_mode` true and false arms, asserting the `low_bandwidth` cookie is `'1'` and `'0'` respectively.

**One worked example, to fix the file's conventions.** The rest follow its shape against the list above.

```python
def test_log_user_in_web_refuses_a_wrong_password_with_a_reset_link(app, db_session, monkeypatch):
    """:47-51. A wrong password on the web arm when `user.password_hash is None`.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.

    The two wrong-password messages differ: :49-51 offers a reset link when
    there is no hash to check against, :52-53 says only 'Invalid password'.
    The exact text is asserted rather than merely that a flash happened,
    because asserting the latter would let the two branches swap undetected --
    false-witness mechanism (d), an input taking the same path under both
    arms.

    password_hash is set to None AFTER set_password, because _seed_login_user
    needs a real hash to exist first for the other tests in this file and
    check_password (app/models.py:1125) is total over a None hash rather than
    raising on it.
    """
    s = _seed_login_user()
    s.user.password_hash = None
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            from flask import session as flask_session
            response = log_user_in(Form('loginuser', 'wrong'), SRC_WEB)
            flashed = [str(message) for _category, message in
                       flask_session.get('_flashes', [])]

    assert response.status_code == 302
    assert any('reset_password_request' in message for message in flashed)
```

Write each remaining test as a full test with a docstring naming its lines. Follow Task 2's conventions exactly — `with app.test_request_context('/'):`, `with _ban_state(monkeypatch):`, the `Form` double.

- [ ] **Step 2: Run and measure**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_auth_login.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_auth_login.py tests/test_redirect_targets.py \
    --cov=app.shared.auth --cov-report=json:/tmp/auth2.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/auth2.json'))['files']['app/shared/auth.py']
print('percent', d['summary']['percent_covered'])
print('missing_lines   ', d['missing_lines'])
print('missing_branches', d['missing_branches'])
"
```

`missing_lines` must be `[]`. `missing_branches` may not be — if any branch remains, name it, say which input would reach it, and if you believe none can, **prove it and say which cause from `tests/README.md` fact 75 you are claiming.** Do not assert equivalence from a failure to kill.

- [ ] **Step 3: Checks and commit**

Duplicate-name check, `git diff --quiet -- app/`, commit, then verify against the commit object with `git show HEAD:tests/test_shared_auth_login.py | /usr/bin/grep -c "^def test_"`.

Subject: `test: cover log_user_in's web arm`. Body: that the arm is dead in production and why it is covered anyway.

---

### Task 4: Dedent the refusal — the round's one production change

**Files:**
- Modify: `app/shared/auth.py:66-75`
- Modify: `tests/test_shared_auth_login.py` (invert three tests)

**Interfaces:**
- Consumes: the four ban-state tests from Task 2.
- Produces: `app/shared/auth.py` refusing every ban state. Task 7 re-measures against it.

**Read the spec's Part 1 before starting.**

- [ ] **Step 1: Re-derive the lines before touching them**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=55 && NR<=80 {printf "%d\t%s\n",NR,$0}' app/shared/auth.py
```

Use what this prints, never a number from this plan.

- [ ] **Step 2: Make the edit**

Dedent `:66-75` by exactly one level (four spaces), so the `if src == SRC_WEB:` / `elif src == SRC_API:` pair hangs off `:57` instead of `:59`. **`:59-64` keeps its body** — banning the new IP is correct and stays inside `:59`. After the edit the block reads:

```python
    if user.id != 1 and (user.banned or user_ip_banned() or user_cookie_banned()):
        # Detect if a banned user tried to log in from a new IP address
        if user.banned and not user_ip_banned():
            # If so, ban their new IP address as well
            new_ip_ban = IpBan(ip_address=ip_address(), notes=user.user_name + ' used new IP address')
            db.session.add(new_ip_ban)
            db.session.commit()
            cache.delete_memoized(banned_ip_addresses)

        if src == SRC_WEB:
            flash(_('You have been banned.'), 'error')

            response = make_response(redirect(url_for('auth.login')))

            # Set a cookie so we have another way to track banned people
            response.set_cookie('sesion', '17489047567495', expires=datetime(year=2099, month=12, day=30))
            return response
        elif src == SRC_API:
            raise Exception('incorrect_login')
```

**From here until this task commits, `git checkout -- app/` is BANNED** — it would silently revert this fix, which is how a real fix was destroyed in sub-project 40. Reverse edits by hand and re-read the restored line with `awk`.

- [ ] **Step 3: Confirm the edit is exactly what was intended**

```bash
cd /home/blentz/git/pyfedi
git diff -- app/shared/auth.py
```

Expected: a pure indentation change on ten lines, no content altered, nothing else in the file touched.

- [ ] **Step 4: Watch exactly three tests fail**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_auth_login.py -v
echo "PYTEST_EXIT=$?"
```

Expected failures, and only these three:

- `test_log_user_in_api_admits_a_banned_user_whose_ip_is_already_banned`
- `test_log_user_in_api_admits_an_ip_banned_user_who_is_not_banned`
- `test_log_user_in_api_admits_a_cookie_banned_user`

**These must keep PASSING**, and they are the point: `test_log_user_in_api_bans_the_ip_of_a_banned_user_and_refuses` (row one, the control) and `test_log_user_in_exempts_the_id_1_account_from_every_ban_check` (`:57`'s first conjunct, which the dedent does not touch).

**A different count or a different set means stop and report.** A fourth failure means the dedent changed something nobody predicted; two means a pin was not pinning.

- [ ] **Step 5: Invert the three**

Rename each from `..._admits_...` to `..._refuses_...`, rewrite the docstring in the present tense, and replace the `result['jwt']` assertion with `pytest.raises(Exception, match='incorrect_login')`. **Add a second assertion to each proving the function reached the guard rather than failing earlier** — a bare `pytest.raises` is false-witness mechanism (c). For row two, assert the `IpBan` count is **still 1 and not 2**, which proves `:59`'s body was correctly skipped while `:75` still fired; for rows three and four, assert no `IpBan` was created at all, which proves the refusal came from the dedented block and not from `:59-64`.

Also update the module docstring: the paragraph beginning "THREE TESTS HERE PIN A DEFECT" now describes history. Say what the lines read before, that they were dedented in this round, and that the three tests below are the inverted pins.

- [ ] **Step 6: Green, then re-measure**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_auth_login.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_auth_login.py tests/test_redirect_targets.py \
    --cov=app.shared.auth --cov-report=json:/tmp/auth3.json -q
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/auth3.json'))['files']['app/shared/auth.py']
print('percent', d['summary']['percent_covered'])
print('missing_lines   ', d['missing_lines'])
print('missing_branches', d['missing_branches'])
"
```

The dedent may open a branch that was unreachable before — `:66`'s false arm is now reachable from three states instead of one. If a new gap appears, close it; **a production change reopens coverage**.

- [ ] **Step 7: Sweep for collateral damage**

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -rln "log_user_in" tests/
```

Run every file this returns. `tests/test_redirect_targets.py` is the one to watch. Report what you ran and what happened, including "nothing changed".

- [ ] **Step 8: Commit**

```bash
cd /home/blentz/git/pyfedi
git diff --stat -- app/
git add app/shared/auth.py tests/test_shared_auth_login.py
git commit -F <message-file>
git status --porcelain
git diff --numstat "$(git rev-parse HEAD~1)" HEAD
```

Subject: `fix: refuse every ban state at login, not only a new IP address`. Body, in prose: the four states and which were admitted; that the refusal was nested inside the new-IP detection; that row one *creates* row two, so a banned user was refused once and admitted thereafter; and that the three tests inverted in this commit were written first against the old behaviour.

---

### Task 5: `app/shared/upload.py`

**Files:**
- Create: `tests/test_shared_upload.py`
- Read: `app/shared/upload.py` (whole, 134 lines), and `tests/test_utils_security.py:993-1049`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

**Target:** 45 statements, 26 arcs, at 44.531%.

**Read the existing harness first.** `tests/test_utils_security.py` already drives `process_upload` for real: it imports it at `:35`, defines `MEDIA_ROOT = 'app/static'` at `:44` and a `files_under(root)` helper at `:47` for before/after comparison, and its tests build file objects with an `_upload(...)` helper and clean up after themselves. Uploads land under `app/static/media/`, which is gitignored. **Copy that arrangement; do not invent a second one**, and include that file in every coverage measurement of this module.

**What needs covering, read at source:**

- `:18` — the empty-file guard, both arms.
- `:22` — `can_upload_video(user)` extending `allowed_extensions`; needs a user with the permission and one without.
- `:25` — extension rejection.
- `:30` — `store_files_in_s3()` choosing the directory.
- `:48-50` — SVG sanitize-or-reject. `tests/test_utils_security.py` already covers much of this; check before duplicating.
- `:67` — the not-svg/not-gif/not-video gate into the Pillow re-encode.
- `:69`/`:84` — `if '.' + img.format.lower() in allowed_extensions` and its `else: raise`. Reaching `:84` needs a file whose **extension is allowed but whose real format is not** — e.g. a GIF saved as `.png`. That mismatch is the whole point of the check.
- `:75`/`:79` — the `image_format` and `image_quality` kwargs, both arms each. These read `current_app.config['MEDIA_IMAGE_FORMAT']` and `['MEDIA_IMAGE_QUALITY']`; set them per test.
- `:89-109` — the S3 branch. `tests/conftest.py:10` already imports `mock_aws` and `:524` already uses it; follow that pattern and set the `S3_*` config keys the code reads.
- `:112` — the `if user:` File/user_file rows, both arms.
- `:127`, `:130` — `process_file_delete`'s two false arms.

**`:120-121` needs a ruling, not an assumption.** `if not url: raise Exception('unable to process upload')`. `url` is assigned at `:86` from an f-string containing `SERVER_URL` and a path, and possibly reassigned at `:106` from another f-string; neither can be empty. **Ask the question this round exists to ask: is there an input that reaches it, or a double that reaches it, or neither?** Record the answer either way in your report. If you conclude nothing can reach it, name which cause from `tests/README.md` fact 75 you are claiming and prove it — do not write "no test failed" and call it equivalent.

- [ ] **Step 1: Write the file**

Module docstring must state: the module was at 44.531%; that `tests/test_utils_security.py` already covers the SVG paths and is part of this module's oracle; and that uploads write under `app/static/media/`, gitignored, with each test cleaning up.

Build real images with Pillow in memory:

```python
import io
from PIL import Image
from werkzeug.datastructures import FileStorage


def _image(fmt='PNG', size=(8, 8), filename=None):
    """A real, small image as an uploadable file object.

    Pillow actually opens and re-encodes this at app/shared/upload.py:68-82,
    so a stub file object would fail there rather than exercising the branch.
    8x8 keeps the thumbnail step at :72 cheap while staying a genuine image.
    """
    buffer = io.BytesIO()
    Image.new('RGB', size, color=(120, 120, 120)).save(buffer, format=fmt)
    buffer.seek(0)
    ext = {'PNG': '.png', 'GIF': '.gif', 'JPEG': '.jpg', 'WEBP': '.webp'}[fmt]
    return FileStorage(stream=buffer, filename=filename or ('probe' + ext))
```

Clean up every file a test writes, in a `finally` or a fixture — the existing file's `files_under` helper is there to make before/after comparison easy.

- [ ] **Step 2: Run, measure with the right oracle, and rule on `:120-121`**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_upload.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_upload.py tests/test_utils_security.py \
    --cov=app.shared.upload --cov-report=json:/tmp/upload.json -q
echo "PYTEST_EXIT=$?"
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/upload.json'))['files']['app/shared/upload.py']
print('percent', d['summary']['percent_covered'])
print('missing_lines   ', d['missing_lines'])
print('missing_branches', d['missing_branches'])
"
```

**`tests/test_utils_security.py` is in that command deliberately.** Report both lists and your ruling on `:120-121`.

- [ ] **Step 3: Confirm no stray files were left behind**

```bash
cd /home/blentz/git/pyfedi
git status --porcelain
```

Expected: only `tests/test_shared_upload.py`. Uploads are gitignored, but a test that writes outside `app/static/media/` would show here.

- [ ] **Step 4: Checks and commit**

Duplicate-name check, `git diff --quiet -- app/`, commit, verify against the commit object.

Subject: `test: cover process_upload and process_file_delete`. Body: the real-image requirement, the S3 branch under moto, the format-versus-extension mismatch that reaches `:84`, and your `:120-121` ruling with its reason.

---

### Task 6: The two survivor recipes

**Files:**
- Modify: `tests/test_shared_user_bans.py` (append)
- Read: `app/shared/user.py:168-207`

**Interfaces:**
- Consumes: `_seed_ban_scenario`, `_BanForm`, `_recording_task_selector`, `no_real_purge`, `redis_lock_only_double` — all already defined in that file by sub-project 43.
- Produces: nothing.

**These add evidence, not coverage.** `app/shared/user.py` measures 100 and **must still measure 100 afterwards**. Both target lines are already executed; what is missing is an assertion that would die if they changed.

- [ ] **Step 1: Close M68 — `ban_user:201`'s `ban_ip_address` flag**

`:201` is `if ban_ip_address and to_ban.ip_address:`. No test covers `(ban_ip_address=False, address present)`, so the first operand can be deleted and the suite stays green while **everyone banned is IP-banned**. Clone `test_ban_user_creates_an_ip_ban`, set the target's `ip_address`, pass `'ban_ip_address': False`, and assert `db.session.query(IpBan).count() == 0`.

The docstring must say this is the first operand of `:201`'s `and`, that the second is covered by the existing `test_ban_user_skips_the_ip_ban_when_the_target_has_no_address`, and that the pair is what makes neither operand deletable.

- [ ] **Step 2: Close M53 and M54 — the remote purge's two calls**

`:175-176` are `to_ban.delete_dependencies()` and `to_ban.purge_content(flush=flush_cdn)`. The branch *selection* is already proven (M49 killed), but both calls can be deleted with the suite green. Add one test that drives a **remote** target through `purge_content=True` and asserts an observable effect of each call. Read the two methods on `User` first and assert on what they actually do — a row they remove, a column they clear. If neither leaves an observable trace in a test database, say so and instead record the calls with a monkeypatch that asserts both were made with the right arguments, explaining in the docstring why a behavioural assertion was not available.

- [ ] **Step 3: Verify the module is still closed**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh tests/test_shared_user_bans.py -v
echo "PYTEST_EXIT=$?"
./run_tests.sh tests/test_shared_user_bans.py tests/test_shared_user_blocks.py \
    tests/test_shared_user_follows.py tests/test_api_user_subscriptions.py \
    tests/test_redirect_targets.py tests/test_instance_util.py \
    --cov=app.shared.user --cov-report=json:/tmp/user-still.json -q
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json
d = json.load(open('/tmp/user-still.json'))['files']['app/shared/user.py']
print('percent', d['summary']['percent_covered'])
print('missing_lines   ', d['missing_lines'])
print('missing_branches', d['missing_branches'])
"
```

**That six-file oracle is the one sub-project 43 established by measurement** — the three `test_shared_user_*` files alone do not execute `subscribe_user`, and omitting `test_redirect_targets.py` or `test_instance_util.py` under-reports `follow_user`/`unfollow_user`. Both lists must be `[]`.

- [ ] **Step 4: Commit**

Subject: `test: pin ban_user's ip-ban flag and its remote purge calls`. Body: which mutants these close (M68, M53, M54), and that the module still measures 100.

---

### Task 7: Floors and the full suite — CONTROLLER ONLY

**Files:**
- Modify: `coverage_floors.ini`

**Only the controller runs this task.** One pytest session at a time, in the foreground, never killed.

- [ ] **Step 1: Full suite with whole-app coverage**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 --cov=app --cov-report=json:/tmp/final.json -q
echo "PYTEST_EXIT=$?"
```

`--cov=app`, not a narrow form: `tests/check_coverage_floors.py`'s `violations()` counts a floored module absent from the report as 0.0, so a narrow JSON reports every other floor as a false violation. Do not pipe — a pipeline eats the exit status. If the run exceeds your tool's timeout, **let it finish in the background**; do not kill it.

- [ ] **Step 2: Read the three modules as lists**

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import json, math
files = json.load(open('/tmp/final.json'))['files']
for p in ('app/shared/domain.py','app/shared/auth.py','app/shared/upload.py'):
    d = files[p]; s = d['summary']
    print(p, s['percent_covered'], 'floor', math.floor(s['percent_covered']))
    print('   missing_lines   ', d['missing_lines'])
    print('   missing_branches', d['missing_branches'])
    print('   partial', s['num_partial_branches'])
"
```

The criterion is `missing_lines == []` and `missing_branches == []` — **not** a percentage that rounds to 100. Sub-project 42 raised a floor to 99 against a module one test short, correctly measured.

- [ ] **Step 3: Add the three floor entries**

Insert beside the siblings in `coverage_floors.ini`, using `floor(percent_covered)` from Step 2:

```ini
app/shared/user.py = 100
app/shared/domain.py = <measured>
app/shared/auth.py = <measured>
app/shared/upload.py = <measured>
```

25 floors total. Floors only ever rise.

- [ ] **Step 4: Full suite again with the floors check chained**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 --cov=app --cov-report=json:/tmp/final.json -q \
  && podman-compose -f compose.test.yaml exec -T test-runner \
       python tests/check_coverage_floors.py /tmp/final.json coverage_floors.ini
echo "CHAIN_EXIT=$?"
```

**Both arguments.** Expected: `All 25 module floors met.` Record pytest's own passed/skipped/duration figures — never a number from this plan.

- [ ] **Step 5: Commit**

Subject: `test: add coverage floors for domain.py, auth.py and upload.py`. Body: the three measured percentages, the empty lists, the partial-branch counts, and the suite's own figures.

---

### Task 8: Mutation pass

**Files:** none permanently. Every mutation is reversed before the next.

**Scope by the STATEMENT list, not the arc table.** Derive the statement and compound lists mechanically:

```bash
cd /home/blentz/git/pyfedi
podman-compose -f compose.test.yaml exec -T test-runner python -c "
import ast
for path in ('/app/app/shared/domain.py','/app/app/shared/auth.py','/app/app/shared/upload.py'):
    tree = ast.parse(open(path).read())
    stmts = sorted({n.lineno for n in ast.walk(tree) if isinstance(n, ast.stmt)})
    compounds = sorted({n.lineno for n in ast.walk(tree) if isinstance(n, ast.BoolOp)})
    print(path, 'STATEMENTS', len(stmts), stmts)
    print(path, 'COMPOUNDS', len(compounds), compounds)
"
```

**Publish that command and its complete raw output beside every count you report.**

- [ ] **Step 1: Measure the oracle before using it**

For each module, confirm your chosen test files reproduce the full-suite figure from Task 7. `auth.py` needs `tests/test_redirect_targets.py`; `upload.py` needs `tests/test_utils_security.py`. **An oracle that does not execute a function turns every mutant there into a survivor** — this happened twice in sub-project 43. Report the comparison.

- [ ] **Step 2: Run the pass**

For each mutation: apply one edit, run the module's oracle, record KILLED or SURVIVED, then **reverse by hand** and re-read the restored line with `awk`. **`git checkout -- app/` is banned for this whole task.**

Rules: a crash kill is not a kill unless a viable non-crashing variant also dies; fix-catching is not a unique kill; an operator can be structurally void; **an equivalence claim needs a proof of unkillability**; non-failures are evidence and every survivor is written up individually.

- [ ] **Step 3: The two mandatory mutations**

1. **Re-nest the fixed refusal** — put `app/shared/auth.py:66-75` back inside `:59`. The **three inverted pins from Task 4 must fail.** If they do not, the fix is decorative.
2. **Neutralise `:57`'s `user.id != 1` to `True`.** **Nothing should die.** If something does, that test is asserting the id-1 exemption by accident — name it, because `test_log_user_in_exempts_the_id_1_account_from_every_ban_check` is supposed to be the only witness and it asserts the opposite direction.

- [ ] **Step 4: Prove the tree is clean**

```bash
cd /home/blentz/git/pyfedi
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git status --porcelain
awk 'NR>=57 && NR<=76 {printf "%d\t%s\n",NR,$0}' app/shared/auth.py
```

The `awk` is there because this task mutates the lines the round just fixed: confirm the dedent survived the pass.

No commit for this task.

---

### Task 9: Register the findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

**Paths and conventions, verified at source** — the register is that file, **not** `docs/superpowers/findings-register.md`, which does not exist. Entries are table rows `| D### | location | description | status | evidence |`. The live next number is the **maximum** over every marker, per the file's own note at `:2333`:

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -o 'Next free number: D[0-9]*' docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md | sort -u -t D -k2 -n | tail -1
```

Expected `D575`. **Do not edit an older marker** — they are historical records; append a new one. `tests/README.md` facts are `**NNN. HEADING` and the last is 250, so start at **251**.

- [ ] **Step 1: Register**

1. **`app/shared/auth.py:57-75` — FIXED this round.** The four-state table, that the refusal was nested inside the new-IP detection, that row one creates row two so a banned user was refused once and admitted thereafter, and that three tests were written against the old behaviour and inverted.
2. **`:57`'s `user.id != 1`** exempts the first account from every ban check — the production face of the id-1 trap. Pinned, not fixed.
3. **`app/shared/auth.py` has a dead `SRC_WEB` arm**, and two functions share the name `log_user_in`.
4. **`:29-33` versus `:24`** — the API arm falls back to email, the web arm does not, so the two accept different credentials.
5. **`block_domain`/`unblock_domain` have no self-block and no admin/staff guard**, and **an unknown domain is a silent no-op** that still returns `user_id` on the API arm.
6. **`upload.py:40`'s `file_size`** is assigned and overwritten at `:85` without being read — a dead store.
7. **`upload.py:120-121`** — whatever Task 5 ruled, with its proof.
8. **Every survivor from Task 8**, each with line, mutation and why nothing killed it.
9. **M68, M53 and M54 closed** by Task 6, with the sub-project 43 entries they discharge.

- [ ] **Step 2: Facts from 251**

Candidates, all verified this round — add the genuinely new ones and **re-derive any count**:

- `from ... import` binds a name into the importing module's globals, so `app/shared/auth.py:14`'s `user_ip_banned` is patched by rebinding `app.shared.auth.user_ip_banned`; patching `app.utils` does not intercept. `app/utils.py:2308`'s `ip_address = get_ip_address` is an alias and follows the same rule.
- A module's coverage is not necessarily produced by the test files named after it: `app/shared/auth.py`'s web arm comes from `tests/test_redirect_targets.py` and `app/shared/upload.py`'s SVG paths from `tests/test_utils_security.py`.
- **The distinction this round turned on**: "no production caller reaches it" is not a reason to stop measuring (D564's retraction), but "no input reaches it because the callee cannot produce the condition" is fact 75's cause 4(c) and stands. Ask which you have.

- [ ] **Step 3: Commit**

Subject: `docs: register sub-project 44's findings and the auth test facts`. Body: how many entries, which were fixed versus pinned, and the D-range used.

---

## Success criteria

- All four ban states pinned, the dedent made, three pins inverted, row one and the id-1 test still passing unchanged.
- `app/shared/domain.py`, `app/shared/auth.py` and `app/shared/upload.py` each at `missing_lines []` and `missing_branches []`, **checked as lists**, with any line ruled unreachable carrying a named cause from `tests/README.md` fact 75 and a proof.
- Three **new** `coverage_floors.ini` entries, 25 floors total.
- `app/shared/user.py` still at 100 after Task 6.
- Full suite green, floors check chained with `&&`, **both** arguments, against a `--cov=app` JSON.
- Findings registered from **D575**; `tests/README.md` facts from **251**.
- **Exactly one production change**: the dedent.

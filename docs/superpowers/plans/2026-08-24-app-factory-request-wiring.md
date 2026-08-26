# App Factory Request Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `create_app()` produce a fully wired application, so tests exercise the same app production runs — and cover the moved request handling to 100%.

**Architecture:** The context processor, 35 Jinja globals and filters, `before_request`, `after_request` and `teardown_appcontext` move from `pyfedi.py` into a new `app/request_hooks.py`, exposed as `register_request_hooks(app)` and called from `create_app()` using the deferred-import pattern the factory already uses for `load_plugins`. `pyfedi.py` becomes a thin entry point, and `profile_app.py`'s duplicate context processor is deleted.

**Tech Stack:** Python 3, Flask 3.1.3, Flask-SQLAlchemy 3.1.1, pytest, pytest-cov, podman-compose.

**Spec:** `docs/superpowers/specs/2026-08-24-app-factory-request-wiring-design.md`

## Global Constraints

- **Hooks move VERBATIM.** Do not refactor, rename, simplify, or "improve" any moved code. Changing behaviour while relocating it makes a regression impossible to attribute. If something looks wrong, note it in the report; do not fix it here.
- **100% branch coverage of `app/request_hooks.py`,** enforced by `--cov-fail-under=100`. See the coverage ruling below for what that does and does not mean.
- **Coverage is a floor, not the goal.** Every test asserts on observable behaviour — a response header, a `g` value, a rendered string, a session flag. A test that merely executes a line to move the number is a defect, and reviewers are told to reject it. If a branch cannot be covered by a test that asserts something real, say so in the report rather than writing a hollow test.
- `create_app()` imports `register_request_hooks` INSIDE the function, matching `from app.plugins import load_plugins`. A module-scope import in `app/__init__.py` creates cycles, because nearly every module imports `app`.
- `/inbox` must keep skipping `g.site`. `before_request` excludes it deliberately so duplicate detection works, and it is easy to lose in a move.
- ActivityPub responses must keep their existing no-session-cookie and caching handling in `after_request`.
- `shell_context_processor` returns `{'db': db, 'app': app}` referring to `pyfedi.py`'s module-level `app`. Inside the factory it must bind the `app` parameter.
- No new RUNTIME dependencies. `pytest-cov` is a test dependency and is the only addition.
- Tests run only via `./run_tests.sh [pytest args]`. There is NO host Python environment — never run `pytest`, `python`, or `flask` on the host. See `tests/README.md`.
- Baseline: `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py` → 278 passed, 0 failed. Must still be 0 failed at every commit.

## Ruling: what "100% coverage on all new code" means here

This change is a **move**. By line diff, `app/request_hooks.py` is entirely new — roughly 130 lines — but almost all of it is existing production code relocated.

The narrow reading (cover only the new `register_request_hooks` wrapper) is satisfied by any test that builds an app, and proves nothing. **The strict reading applies: 100% branch coverage of every line in `app/request_hooks.py`.**

That is the demanding interpretation on purpose. This code has never had a test, and covering it is most of the value of the change — the move alone only relocates untested code.

Consequence to accept: this is not a small refactor. Task 3 is the first real test suite for PieFed's request handling, and it is larger than the move itself.

## Coverage command

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py \
    --cov=app/request_hooks.py --cov-branch --cov-report=term-missing --cov-fail-under=100
```

`--cov-branch` matters: `after_request` is dense with conditionals, and line coverage alone would report 100% while leaving whole branches unexercised.

---

> **CORRECTION (2026-08-25, final-fix wave).** Where this plan says to append a
> test dependency to `requirements.txt`, that instruction is WRONG and was
> followed. `requirements.txt` is the production install -- the Dockerfile's
> `builder` stage, `deploy.sh` and INSTALL.md all run
> `pip install -r requirements.txt` -- and `atheris`, added by the fuzzing task,
> publishes no aarch64 wheel, so it broke `pip install -r requirements.txt` on
> every ARM64 host. Test dependencies now live in `requirements-test.txt`,
> installed by the Dockerfile's `test` stage, which is what `compose.test.yaml`
> builds. The plan text below is left as written because it is the record of
> what was planned; do not follow this part of it.


## File Structure

| File | Responsibility | Change |
|---|---|---|
| `requirements.txt` | add `pytest-cov` | Modify |
| `app/request_hooks.py` | every request-time and template-time registration, plus `register_request_hooks(app)` | Create |
| `app/__init__.py` | call the registrar from `create_app()` | Modify |
| `pyfedi.py` | reduced to a thin entry point | Modify |
| `profile_app.py` | delete the duplicate context processor | Modify |
| `tests/test_request_hooks.py` | cover `app/request_hooks.py` to 100% branch | Create |
| `tests/conftest.py` | whatever the teardown hook forces (see Task 3) | Modify |
| `tests/README.md` | document the coverage command | Modify |

---

## Task 1: Coverage tooling

**Files:**
- Modify: `requirements.txt`
- Modify: `tests/README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: `pytest-cov` available inside the `test-runner` container, and a documented coverage command.

The image must be rebuilt for this, since requirements are installed at build time.

- [ ] **Step 1: Add the dependency**

Append to `requirements.txt`, keeping it beside the existing `pytest` entry:

```
pytest-cov
```

- [ ] **Step 2: Rebuild the test image and confirm the plugin loads**

```bash
./run_tests.sh --down
podman-compose -f compose.test.yaml build test-runner
./run_tests.sh tests/test_visibility.py -q --cov=app/activitypub/util.py --cov-report=term-missing
```

Expected: the run passes AND a coverage table is printed. If the table is absent, the plugin did not install — stop and report rather than continuing.

Note `--down` destroys the tmpfs database, so this run replays ~269 migrations and is slow exactly once.

- [ ] **Step 3: Document the command**

Add to `tests/README.md`, under the existing run instructions:

```markdown
## Coverage

`app/request_hooks.py` is held at 100% branch coverage:

    ./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py \
        --cov=app/request_hooks.py --cov-branch --cov-report=term-missing --cov-fail-under=100

Branch coverage, not just line coverage: `after_request` is dense with
conditionals, and line coverage alone reports 100% while leaving whole branches
unexercised.

Coverage is a floor, not a target. A test that executes a line without asserting
anything raises the number and catches nothing.
```

- [ ] **Step 4: Commit**

```bash
git add requirements.txt tests/README.md
git commit -m "test: add pytest-cov"
```

---

## Task 2: Move the wiring into the factory

**Files:**
- Create: `app/request_hooks.py`
- Modify: `app/__init__.py` (inside `create_app`, near the existing `from app.plugins import load_plugins`)
- Modify: `pyfedi.py`
- Modify: `profile_app.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `register_request_hooks(app) -> None` in `app/request_hooks.py`, called by `create_app()`.

**This task moves code verbatim.** TDD's usual order does not apply and that is deliberate: you cannot write a failing test for code that already works in production and cannot currently be executed under test at all. Task 3 supplies the characterisation tests, and any behaviour difference it finds is a move error to fix there.

- [ ] **Step 1: Create the module**

Create `app/request_hooks.py` containing, in this order:

1. The imports `pyfedi.py:1-24` currently uses for these hooks — `datetime`, `os`, `flask`, `get_locale`, `ngettext`, `current_user`, `generate_csrf`, `pendulum`, `session, g, json, request, current_app`, `text`, the `app.constants` names, `Site`, and the `app.utils` names. Take them from `pyfedi.py`; do not guess.
2. `def register_request_hooks(app):` containing, verbatim and in this order:
   - the `@app.context_processor` function from `pyfedi.py:30-42`
   - the `@app.shell_context_processor` from `pyfedi.py:44-46`, with `{'db': db, 'app': app}` now binding the `app` parameter
   - the 35 `jinja_env` assignments from `pyfedi.py:48-85`, **without** the `with app.app_context():` wrapper — inside the factory the app is being constructed and no context is needed
   - the `@app.before_request` from `pyfedi.py:86-127`
   - the `@app.after_request` from `pyfedi.py:130-181`
   - the `@app.teardown_appcontext` from `pyfedi.py:183-187`

Each decorated function is defined inside `register_request_hooks` and decorated with `@app.<hook>`, so it closes over the `app` parameter.

- [ ] **Step 2: Call it from the factory**

In `app/__init__.py`, in `create_app()`, immediately before `return app` and beside the existing plugin load:

```python
    from app.request_hooks import register_request_hooks
    register_request_hooks(app)

    return app
```

The import is inside the function deliberately. `app/__init__.py` is imported by nearly every module, and `request_hooks` imports `app.models` and `app.utils`; a module-scope import here creates a cycle.

- [ ] **Step 3: Reduce pyfedi.py**

Delete lines 28-187 of `pyfedi.py` — the context processor, shell context processor, jinja block, `before_request`, `after_request` and `teardown_appcontext`. Keep `app = create_app()` and `cli.register(app)`.

Then remove imports left unused. Do not guess which: run the import check in Step 5 and remove what `pyflakes`-style inspection or a failed import reveals. Leaving an unused import is harmless; removing a still-used one breaks startup.

- [ ] **Step 4: Delete the duplicate in profile_app.py**

Remove the `@app.context_processor` block at `profile_app.py:27-40`. It is a stale copy — it lacks `can_detect_ai`, which `pyfedi.py` has — and after this change the factory supplies it. Remove imports it alone used.

- [ ] **Step 5: Verify every entry point still imports**

```bash
podman-compose -f compose.test.yaml exec -T test-runner python -c "import pyfedi; print('pyfedi ok')"
podman-compose -f compose.test.yaml exec -T test-runner python -c "import profile_app; print('profile_app ok')"
podman-compose -f compose.test.yaml exec -T test-runner python -c "import celery_worker_docker; print('celery ok')"
```

Expected: all three print ok. A circular-import error here means Step 2's import was placed at module scope instead of inside `create_app()`.

- [ ] **Step 6: Confirm the suite still passes**

Run: `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py`
Expected: 278 passed, 0 failed.

If tests now fail with detached-instance or session errors, that is `teardown_appcontext`'s `db.session.remove()` firing when Flask pops the app context around each test-client request. **Fix it in `tests/conftest.py`** — by re-querying after requests, or adjusting fixture scope. Do NOT weaken or skip the teardown hook: production depends on it, and changing it to make a test pass inverts the point of this change.

- [ ] **Step 7: Commit**

```bash
git add app/request_hooks.py app/__init__.py pyfedi.py profile_app.py tests/conftest.py
git commit -m "refactor: move request wiring into the app factory"
```

---

## Task 3: Cover the moved hooks to 100%

**Files:**
- Create: `tests/test_request_hooks.py`
- Modify: `tests/conftest.py` if a fixture is needed

**Interfaces:**
- Consumes: `register_request_hooks(app)` from Task 2; the `app` and `db_session` fixtures; `tests/factories.py`.
- Produces: nothing later tasks consume.

This is the first real test suite for PieFed's request handling, and it is larger than the move. Every test asserts on observable behaviour — a header, a `g` value, a session flag, a rendered string. **A test that executes a line without asserting anything is a defect.**

- [ ] **Step 1: Prove a route can now render**

This is the deliverable the whole change exists for. Create `tests/test_request_hooks.py`:

```python
"""Coverage for app/request_hooks.py.

Before this wiring moved into create_app(), no test could render a page: the
context processor, jinja globals and before_request lived in pyfedi.py, outside
the factory, so a test app had none of them and any render raised
AttributeError: site.
"""

from tests.factories import make_instance, make_site, make_user


def test_a_page_renders_under_test(app, db_session):
    """The point of the whole change: a route renders in the test app."""
    make_instance('test.piefed.local', software='piefed')
    make_site()

    with app.test_client() as client:
        response = client.get('/')

    assert response.status_code in (200, 302)
```

- [ ] **Step 2: Run it**

Run: `./run_tests.sh tests/test_request_hooks.py -q`
Expected: PASS. If it still raises `AttributeError: site`, Task 2's registration did not take effect — stop and report.

- [ ] **Step 3: Find what is uncovered**

```bash
./run_tests.sh tests/test_request_hooks.py -q --cov=app/request_hooks.py --cov-branch --cov-report=term-missing
```

Read the `Missing` column. It lists every uncovered line and branch, and it is your worklist for Step 4.

- [ ] **Step 4: Cover each branch with a behavioural assertion**

Write one test per branch the report names. The branches that exist, and what each must assert:

**`before_request`**
- `OPTIONS` request returns 200 with `Access-Control-Allow-Origin` set, and does NOT reach the route
- `g.nonce`, `g.locale`, `g.low_bandwidth` are set on a normal request
- `low_bandwidth` cookie set to `'1'` makes `g.low_bandwidth` true
- `/inbox` does NOT get `g.site` — assert `hasattr(g, 'site')` is False. This is deliberate behaviour and a test protecting it is the point
- a `/static/` path also skips `g.site`
- `g.admin_ids` is populated when the setting is absent, and read from the setting when present
- an authenticated request updates `last_seen`
- an anonymous request with `Windows` in the user agent sets `current_user.font` to `'inter'`, and to `''` otherwise
- an anonymous request with an external `Referer` stores it in the session; one from this server does not

**`after_request`**
- CORS headers present on a normal response
- a `/static/` path gets `Cache-Control: public, max-age=31536000`
- an `application/activity+json` response does not set a session cookie
- `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY` on a normal HTML response
- an `/embed` path does NOT get `X-Frame-Options`
- a CSP header with the nonce for an authenticated non-htmx request
- no CSP for an `HX-Request: true` request
- `Strict-Transport-Security` when `HTTP_PROTOCOL` is `https`, absent otherwise — override config in the fixture
- `Link` rel=license header when `ALLOW_AI_CRAWLERS` is false, absent when true
- `/api/` paths get `Cache-Control: no-store`
- an authenticated HTML response gets the no-store cache header; an anonymous one does not
- `Vary` includes `Accept-Language` and `Cookie` on an HTML response

**context processor** — assert a rendered template can use a value it supplies, and that `site` and `nonce` fall back to `None` when `g` lacks them.

**`teardown_appcontext`** — assert the session is removed after an app context pops, and that the exception path rolls back. Push a context, raise inside it, and observe.

**`shell_context_processor`** — call it and assert it returns `db` and the app.

- [ ] **Step 5: Reach the gate**

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py \
    --cov=app/request_hooks.py --cov-branch --cov-report=term-missing --cov-fail-under=100
```

Expected: 0 failed, and coverage 100%.

If a branch genuinely cannot be exercised by a test that asserts something real, do NOT write a hollow test to reach the number. Report the branch, why it is unreachable, and what you would need. An honest 98% with a named gap is worth more than 100% padded with assertion-free tests.

- [ ] **Step 6: Commit**

```bash
git add tests/test_request_hooks.py tests/conftest.py
git commit -m "test: cover request hooks to 100% branch coverage"
```

---

## Task 4: Verify against the running application

**Files:** none — this task changes nothing.

**Interfaces:**
- Consumes: everything above.
- Produces: a report entry recording what was checked.

**The suite does not prove this change works.** All 278 tests passed before any of this wiring existed, which is the whole problem. A green suite proves only that nothing already-covered regressed. The spec lists five checks that require actually running the app.

- [ ] **Step 1: Start the development stack**

```bash
podman-compose -f compose.dev.yaml up -d
```

If the developer's dev stack must not be touched, STOP and report — hand back the checklist below instead of guessing.

- [ ] **Step 2: Work the checklist**

1. Load the home page — exercises `before_request`, the context processor, the jinja globals and `after_request` together.
2. Log in — exercises the authenticated branch of `before_request` and the CSP nonce path.
3. Load `/admin/federation` — a page with forms, recently changed.
4. `curl -H 'Accept: application/activity+json'` a post or user URL; confirm no `Set-Cookie` and the existing cache headers.
5. `curl -X POST` to `/inbox` with any body; confirm it is handled and that `g.site` skipping did not break it.

- [ ] **Step 3: Record the results**

Write what was checked and what was observed into the task report — each of the five, with the actual response or observation, not a claim that it "looked fine".

- [ ] **Step 4: Stop the stack if you started it**

```bash
podman-compose -f compose.dev.yaml down
```

---

## Self-Review Notes

Spec coverage:

| Spec section | Task |
|---|---|
| Extract to `app/request_hooks.py`, registrar called from `create_app` | Task 2 |
| Deferred import to avoid cycles | Task 2 Step 2 |
| `pyfedi.py` reduced to entry point | Task 2 Step 3 |
| `profile_app.py` duplicate removed | Task 2 Step 4 |
| Hooks move verbatim | Global Constraints, restated in Task 2 |
| Celery workers gain a teardown hook | Task 2 Step 5 verifies they still import |
| `teardown_appcontext` may detach objects in tests | Task 2 Step 6, with the fix directed at the fixture |
| `shell_context_processor` binds the `app` parameter | Global Constraints, Task 2 Step 1 |
| `/inbox` keeps skipping `g.site` | Global Constraints, and a test in Task 3 Step 4 |
| Verification by running the app | Task 4 |
| A route test proving the purpose is achieved | Task 3 Step 1 |
| 100% coverage of new code | Ruling, Task 1, Task 3 Step 5 |

Out-of-scope items in the spec (refactoring the hooks, writing broader route tests, `fastapi_server.py`) correctly have no task.

Symbols are consistent throughout: `register_request_hooks(app)`, `app/request_hooks.py`, `tests/test_request_hooks.py`.

Known risk carried into implementation: Task 3's branch list was derived by reading `pyfedi.py:86-187`. If the coverage report in Step 3 names a branch the list omits, cover it too — the report is authoritative, not this plan.

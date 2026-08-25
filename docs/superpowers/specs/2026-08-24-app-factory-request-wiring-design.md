# Moving Request Wiring Into the App Factory

Date: 2026-08-24
Status: Approved design, ready for implementation planning

## Problem

`create_app()` does not produce a working application. It builds one, and then
`pyfedi.py` attaches the request-time and template-time wiring to the object it
returns. Anything else that calls `create_app()` gets an app missing all of it.

Registered in `pyfedi.py`, outside the factory:

| Location | Registration |
|---|---|
| `pyfedi.py:30` | `@app.context_processor` — 20+ template globals |
| `pyfedi.py:44` | `@app.shell_context_processor` |
| `pyfedi.py:50` | `app.jinja_env.globals['len']` |
| `pyfedi.py:79-84` | six `jinja_env.filters` |
| `pyfedi.py:86` | `@app.before_request` |
| `pyfedi.py:130` | `@app.after_request` |
| `pyfedi.py:183` | `@app.teardown_appcontext` |

Four other modules call `create_app()`: `celery_worker.default.py`,
`celery_worker_docker.py`, `run_nntp.py`, and `profile_app.py`.

### What this costs

**No route can be tested.** `tests/conftest.py` builds its app from the factory, so
`g.site`, `g.nonce`, `g.admin_ids`, the template globals and the filters are all
absent. Any test that renders a page raises `AttributeError: site`. This blocked
route-level tests twice in recent work — for `/u/<actor>` remote handle resolution
and for the Mastodon directory scan — and in both cases the behaviour had to be
verified by reading the code instead.

**The duplication has already drifted.** `profile_app.py:27` carries its own copy of
the context processor. It is missing `can_detect_ai`, which `pyfedi.py:36` has. Two
copies of the same 20-line dict, already out of step. Nothing detects this.

The deeper problem is that tests exercise a differently-assembled application than
production runs. That is the same class of gap that let a defect ship recently: the
boost feed tests passed against a `Post` shape ingestion never produces.

## Approach

Extract the wiring into `app/request_hooks.py`, exposing a single
`register_request_hooks(app)`. `create_app()` calls it near the end, beside the
existing `load_plugins()` call. `pyfedi.py` becomes a thin entry point.

Rejected alternatives:

- **Inline it all into `create_app()`.** Adds roughly a hundred lines to a factory
  already 230 lines long, and mixes request-time concerns into app assembly. No
  advantage over a separate module.
- **Register the hooks on a Blueprint.** These are application-wide, and
  `shell_context_processor` does not exist on blueprints. Wrong tool.
- **Replicate the wiring in the test fixture.** Zero production risk, but creates a
  third copy that drifts silently — and silent drift is precisely the failure this
  design exists to remove.

### Circular imports

This is the hazard. `before_request` needs `Site`, `get_site_as_dict`, `get_setting`,
`set_setting`, `db`, `current_user`, `get_locale`, `gibberish` and `ROLE_ADMIN`;
`app/__init__.py` is imported by nearly every module in the project, so importing
those at its module scope would create cycles.

`create_app()` already solves this for plugins: `from app.plugins import load_plugins`
sits *inside* the function, deferring the import to call time. `register_request_hooks`
is imported the same way. `app/request_hooks.py` may then import freely at its own
module scope.

## Scope

### In scope

Move all seven registration groups listed above into `app/request_hooks.py`, call the
registrar from `create_app()`, reduce `pyfedi.py` to an entry point, and delete the
duplicate context processor in `profile_app.py`.

### Out of scope

- **Refactoring the hooks themselves.** `after_request` is a long function with
  branching cache and CSP logic. It moves verbatim. Changing behaviour while moving
  code makes a regression impossible to attribute.
- **Adding route tests.** This change unblocks them; writing them is separate work.
- **`fastapi_server.py`.** It is a separate FastAPI application and does not call
  `create_app()`.

## Consequences to accept deliberately

**Celery workers gain request hooks they have never had.** Both worker entry points
call `create_app()`. They do not serve requests, so `before_request` and
`after_request` will not fire — but `teardown_appcontext` will, whenever an app
context pops. Worth stating plainly rather than discovering later.

**`teardown_appcontext` is the risky one for tests.** It calls `db.session.remove()`.
Flask pushes and pops an app context around each test-client request, so after this
change every such request ends by detaching the session. A test that makes a request
and then asserts on a model object it built beforehand may find that object detached.
`tests/conftest.py`'s `app` fixture is session-scoped and holds one app context for
the whole run, which does not prevent per-request contexts from popping inside it.

If this causes failures, the fix is in the test fixture — re-querying after a request,
or scoping differently — not in the teardown hook. Weakening a hook that production
depends on, to make a test pass, would invert the point of this change.

## Verification

**The suite is necessary but not sufficient.** All 278 tests currently pass without
any of this wiring, which is the whole problem: they exercise almost nothing it
touches. A green suite after this change proves only that nothing regressed in what
was already covered.

The real check is running the application:

1. Start the dev stack and load the home page — exercises `before_request`, the
   context processor, the filters and `after_request` together.
2. Log in — exercises the `current_user.is_authenticated` branch of `before_request`
   and the CSP nonce path in `after_request`.
3. Load `/admin/federation` — a page with forms, and one recently changed.
4. Request an object with an ActivityPub `Accept` header and confirm the response is
   still uncached and carries no session cookie, which is the `after_request` branch
   most easily broken by a careless move.
5. Confirm `/inbox` still skips `g.site` — `before_request` deliberately excludes it
   so duplicate detection works, and that exclusion is easy to lose in a move.

Then confirm a route test can now render a page. That is the deliverable, and until
one exists the change has not demonstrably achieved its purpose. One throwaway test
rendering any simple page is enough to prove it.

## Risks

- **Blast radius is the production entry point.** A small diff here can take the
  whole site down, for a payoff that is test infrastructure rather than user-facing
  behaviour. This argues for moving code verbatim and verifying by running the app.
- **`shell_context_processor` returns `{'db': db, 'app': app}`**, referring to
  `pyfedi.py`'s module-level `app`. Inside the factory it must bind the `app`
  parameter instead. Easy to move without noticing.
- **Import-time side effects.** `pyfedi.py:50` runs
  `with app.app_context(): app.jinja_env.globals['len'] = len`. Inside the factory the
  app context wrapper is unnecessary; keeping it would push a context during app
  construction. It should be set directly.

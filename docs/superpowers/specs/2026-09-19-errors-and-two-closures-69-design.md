# Sub-project 69: `app/errors/handlers.py`, and two one-line closures

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `5d68164e1`
**Predecessor:** sub-project 68, which closed `app/tag` — 45 floors.

## Goal

Cover the four error handlers, repair the one defect in them, and take floors on
two modules that are a single statement each from complete. Three new floors,
one of which **closes `app/errors`**.

## Why three modules in one round

`app/errors/handlers.py` is 23 statements with **4 missing lines and 1 missing
arc** — far below the band a round is usually worth. Two other modules are in
the same position for different reasons, and neither justifies a round of its
own:

| module | statements | missing | why it is short |
|---|---|---|---|
| `app/errors/handlers.py` | 23 | 4 lines, 1 arc | three of the four handlers are never reached by the suite |
| `app/admin/constants.py` | 10 | 1 line | `ReportTypes.get_choices()` is called by a form the suite never builds |
| `app/nntp/__init__.py` | 2 | 2 lines | the blueprint is never imported, so the module never executes |

Taken together they are a normal round's worth of work, and the alternative is
three rounds of ceremony around one assertion each.

`app/nntp/__init__.py` reaching 100 does **not** close `app/nntp`:
`server.py` (815 lines) and `nntpserver.py` (1034) are both at 0.0 and are their
own sub-project later. A per-module floor is exactly what the ratchet is for.

## THE HARNESS PROBLEM, WHICH COMES FIRST

Three of the four handlers are only reachable from a request that fails, and the
suite has no failing routes. Two things get in the way:

1. **`app.route` cannot be called after the first request** — `AssertionError:
   The setup method 'route' can no longer be called on the application`, because
   `tests/conftest.py`'s `app` fixture is session-scoped. A test cannot add a
   route that raises.
2. **`TESTING = True` propagates exceptions**, so a raising view re-raises out
   of the test client instead of reaching the 500 handler.

The way through is to make an **existing** route fail: patch the
`render_template` a cheap route uses with a `side_effect`, and set
`PROPAGATE_EXCEPTIONS = False` for the duration. The full request lifecycle then
runs — which matters, because `errors/401.html` and `errors/500.html` extend
`base.html` and need `g.site`, and `g.site` is set by a `before_request` hook
that `app.test_request_context()` never runs. Measured both ways: through
dispatch the templates raise `UndefinedError: 'flask.ctx._AppCtxGlobals object'
has no attribute 'site'`; through a real request they render.

## THE ONE PRODUCTION CHANGE

### P1 — the ordinary 404 is a plain-text string

```python
    if (request.path.startswith('/static/') or ... or request.path.endswith((...))):
        return render_template('errors/404.html'), 404
    cms_page = CmsPage.query.filter(CmsPage.url == request.path).first()
    if cms_page:
        return render_template('cms_page.html', page=cms_page)
    # Fall back to standard 404 page
    return 'not found', 404
```

The comment says "Fall back to standard 404 page". It does not.

**Probes** — an unmatched content path, then an unmatched image path:

```
PROBE q1 status: 404      PROBE q1 body: b'not found'
PROBE q2 status: 404      PROBE q2 body starts: b'\n<p>Oops, something is broken!</p>...'
```

So the branch that exists to **skip** the pretty page — static files, API paths,
asset extensions — is the one that renders it, and a reader who mistypes a URL
gets nine bytes of unstyled text. That is backwards: those are exactly the
requests where a rendered page is wasted, and a person typing a URL is the one
case where it is not.

**Fix:** render `errors/404.html`, which is what the comment already promises and
what the fast path already returns.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:16-17` | **The extension check is case-sensitive**, so `/nosuch.PNG` misses the fast path and pays for a `CmsPage` query: `PROBE q4 status: 404`, `is html: False` before P1. With P1 the two paths return the same page, so the only remaining cost is the query. | A performance shape once P1 lands, not a correctness one. |
| R2 | `:9`, against `:30`, `:36` | **The 404 handler does not roll back** while the 500 and 401 handlers do. | A 404 is raised by `abort()` far more often than by a failure, so a blanket rollback there would discard work the request meant to keep. Registered as a question, not a bug. |
| R3 | `:1` | **Error pages use `flask.render_template`, not `app.utils.render_template`**, so they get no theme, no protocol replacement and no ETag handling. | No theme in the repo ships an `errors/` template, so the theme-aware version would fall through to the same file today. Named so the next reader knows it was checked rather than missed. |
| R4 | `app/templates/errors/429.html` | **The 429 page is plain text** — `b'\n429 - Too Many Requests\n'` — while 401 and 500 render full pages. | A template, not this module. |

## What the rollback rows assert

`db.session.rollback()` in two handlers is the shape D832 registered as
unkillable — except here it is not. A session poisoned by a failed statement
raises on its next use, and the handler's rollback is what clears it:

```
PROBE u2 poisoned: InternalError          (control: no handler in the way)
PROBE u1 status: 500, usable after: 0     (the handler ran and the session works)
```

That is the behaviour the call exists for, and it is observable, so the mutant
deleting it dies on evidence rather than being registered.

## Success criteria

- All three modules at `[]`/`[]` on the **full-suite** run.
- Three new floors of 100; **`app/errors` closes** — 48 floors.
- P1 lands with its pin inverted; `git diff --numstat` names exactly
  `app/errors/handlers.py`.
- Suite green; floors checked with `&&`; a mutation pass over the handlers, with
  anchors checked for uniqueness first — D833.
- Findings registered from **D842**; `tests/README.md` facts from **327**.

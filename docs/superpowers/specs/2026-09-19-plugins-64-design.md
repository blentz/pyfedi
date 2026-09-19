# Sub-project 64: `app/plugins` — closing the package

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `f3b915ebf`
**Predecessor:** sub-project 63, which closed `app/instance` — 40 floors.

## Goal

Cover the plugin system — the loader, the hook registry and the example plugin
every third-party plugin is copied from — repair the defect probed while
scoping it, and **take a floor on each of the three files, which closes
`app/plugins`.**

## Targets

From the full-suite JSON at the delivered tree:

| file | percent | missing statements | missing arcs | total |
|---|---|---|---|---|
| `app/plugins/__init__.py` | 43.59 | 43 | 23 | 66 |
| `app/plugins/example_plugin/__init__.py` | 47.368 | 17 | 13 | 30 |
| `app/plugins/hooks.py` | 80.282 | 11 | 3 | 14 |
| **total** | | **71** | **39** | **110** |

Inside the campaign's 110-130 band, and it closes the package.

## THE PRODUCTION CHANGE

### P1 — `FLASK_DEBUG=true` stops every plugin loading

```python
if int(os.environ.get('FLASK_DEBUG', '0')):
```

**Nine copies**: twice in `app/plugins/__init__.py`, twice in `hooks.py`, and
seven times in `example_plugin/__init__.py`.

**Probe:**

```
PROBE g1 registration exception: ValueError invalid literal for int() with base 10: 'true'
```

The failure mode is the part that matters. Hook registration happens while a
plugin is being **imported**, and `load_plugins` wraps each import in
`except Exception`, which logs `Failed to load plugin <name>` and moves on. So
with `FLASK_DEBUG=true` the system does not crash — **every plugin silently
does not load**, and the only trace is a log line that names the plugin rather
than the cause.

This repo's own `compose.dev.yaml` sets `FLASK_DEBUG=1`, so the documented
configuration works. `true` is the spelling Flask's own documentation uses,
and it is what a docker-compose file or a `.env` written from memory contains.

**Fix: a tolerant reading, in all nine places.** A small module-level helper in
`hooks.py` — `debug_logging_enabled()` — that accepts `1`, `true`, `True`,
`yes` and treats anything else as off, imported by the loader and by the
example plugin. **The example plugin's seven copies are included deliberately**:
it is the file plugin authors copy from, so a bug left there is a bug shipped
to every plugin written afterwards.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `hooks.py:78-82` | **`fire_hook` swallows every handler exception and continues with the PREVIOUS result.** Probe: three handlers, the middle one raising, gave `['first', 'last']` — the caller cannot tell that a plugin failed. Deliberate isolation, and the log line is there; registered as the D720 silent-failure family so the decision is visible. | The isolation is the point of a plugin system. |
| R2 | `hooks.py:80`; `app/api/alpha/utils/post.py:1472`; `app/community/routes.py:1085` | **A handler returning `None` nulls the data for every later handler and for the caller** — `PROBE g4 result: None`. And **both `before_post_create` call sites discard `fire_hook`'s return value**, although the docstring promises "modified data after all handlers have processed it". So a before-hook cannot do what it is documented to do, and a badly written one cannot break anything either. | Whether a plugin may rewrite a post before it is created is a product and security decision. Covered as behaviour. |
| R3 | `__init__.py:82` | **`load_plugins` returns the module global itself**, not a copy — unlike `get_loaded_plugins`, which copies — and never clears it, so repeated calls accumulate. Probe: `PROBE g5 same object: True`. | A shape, not a defect: the loader runs once at app start. Registered with the asymmetry named. |
| R4 | `__init__.py:97-152` | **`reload_plugin` removes a plugin's hooks by matching `func.__module__` against `app.plugins.<name>`**, which is the module name `load_plugins` gives the spec — so a plugin whose hooks were registered from a differently named module keeps them. | Reload is a development convenience; registered with the coupling named. |

## Success criteria

- All three files at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- **Three floors** — which closes `app/plugins` — 43 floors.
- P1 lands with its pin inverted; `git diff --numstat` names exactly the three
  files in `app/plugins`.
- Suite green; floors checked with `&&`; a mutation pass over the package, per
  D602.
- Findings registered from **D807**; `tests/README.md` facts from **320**.
- `tests/test_zz_plugins_probe.py` deleted before delivery.
- **The hook registry is global mutable state**, so every test clears it —
  `clear_hooks()` exists for exactly that and the tests use it in a fixture,
  not by hand, or a forgotten call leaks handlers into the next test.

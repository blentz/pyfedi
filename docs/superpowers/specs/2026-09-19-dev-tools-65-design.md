# Sub-project 65: `app/dev/routes.py` — closing the package

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `4e9ace8f0`
**Predecessor:** sub-project 64, which closed `app/plugins` — 43 floors.

## Goal

Cover the developer tools page — the four fixture generators behind
`current_app.debug` and the ActivityPub replay form — repair the three defects
probed while scoping it, and **take a floor, which closes `app/dev`**
(`__init__.py` and `forms.py` are already at 100).

## Targets

From the full-suite JSON at the delivered tree (`app/dev/routes.py` 13.75%,
118 statements):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `tools` | `:24-192` | 85 | 38 | 123 |
| `tools_activitypub` | `:198-211` | 11 | 4 | 15 |
| **total** | | **96** | **42** | **138** |

Above the band by eight, taken whole because it closes the package.

## THE HARNESS GATE, WHICH COMES FIRST

Both routes begin `if not current_app.debug: abort(404)`, and the suite runs
with debug off. Every test here sets `app.debug = True` in a fixture that
restores it, because a bare `app.debug = True` left behind changes how other
tests' errors propagate. Measured: `PROBE h1 debug off: 404`, `debug on: 200`.

## THE THREE PRODUCTION CHANGES

### P1 — the deletion counter counts nothing

```python
topics_with_communities = 0
deleted_topics = 0

for t in dev_topics:
    ...
    if topic.num_communities == 0:
        db.session.delete(topic)
        db.session.commit()
    else:
        topics_with_communities += 1
...
flash(_(f'{deleted_topics} Dev Topics Deleted.'))
```

`deleted_topics` is initialised, reported, and **never incremented**.

**Probe** — three dev topics, all deletable:

```
PROBE h3 flash: 0 Dev Topics Deleted.
PROBE h3 topics left: 0
```

Every topic was deleted and the developer was told none were. **D736's shape**:
an action observable only through a stale counter.

**Fix:** increment it where the delete happens.

### P2 — populating topics with no communities is a 500

```python
communities = Community.query.filter_by(banned=False)
rand_communities = []
for c in range(10):
    rand_communities.append(random.choice(communities.all()))
```

**Probe:** `PROBE h2 exception: IndexError Cannot choose from an empty sequence`.

A fresh dev instance has no communities, which is exactly when a developer
reaches for this page — and the button above it is the one that creates them.

**Fix:** refuse with a flash naming what to do first, and redirect to the tools
page. The tool's whole purpose is to save a developer time; a traceback does
the opposite.

### P3 — the flashes cannot be translated

```python
flash(_(f'{deleted_topics} Dev Topics Deleted. {topics_with_communities} ...'))
flash(_(f'{deleted_topics} Dev Topics Deleted.'))
```

An f-string is interpolated **before** `gettext` sees it, so the catalog is
asked for a string containing this run's numbers and never matches. Both lines
are being rewritten for P1 anyway.

**Fix:** `ngettext`/`%(num)d` placeholders, the form the rest of this codebase
uses — the same file's community-deletion flash already does it correctly.

`tools_activitypub`'s `_('Invalid json ' + str(e))` has the same fault and is
**registered** rather than fixed: it is a line this round would not otherwise
touch, and what an exception's text should look like to a user is its own
decision.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:209` | **`_('Invalid json ' + str(e))` concatenates before translating**, so that message cannot be translated either — and it puts a parser's exception text in front of a user. | P3's decision, kept narrow. |
| R2 | `:77-81`, `:116-121` | **`random.choice` is called ten times over the same list, with replacement**, so the ten "random communities" can contain duplicates — and then two topics are assigned to one community, leaving a topic with no communities that the delete tool will later report as "still has communities". | Dev scaffolding whose randomness is the point; registered so the duplicate is not read as a database fault. |
| R3 | `:206` | **`replay_inbox_request(j)` is called with only `JSONDecodeError` caught**, so any other failure inside the replay is a 500 on the tools page rather than a flash. | The error contract for replay belongs with whoever owns the inbox. |
| R4 | `:33`, `:71`, `:127`, `:152` | **The four buttons are dispatched by an if/elif chain on `<form>.<field>.data`**, so two buttons submitted together silently run only the first. | Not reachable from the rendered page, which has four separate forms. Registered as a shape. |

## Success criteria

- Both functions at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- **`app/dev/routes.py` takes a floor** at the measured figure rounded down,
  which closes `app/dev` — 44 floors.
- The three repairs land with pins inverted; `git diff --numstat` names exactly
  `app/dev/routes.py`.
- Suite green; floors checked with `&&`; a mutation pass over the module, per
  D602.
- Findings registered from **D813**; `tests/README.md` facts from **321**.
- `tests/test_zz_dev_probe.py` deleted before delivery.

# Sub-project 62: `app/search/routes.py` — closing the package, and unblocking full-text search for the whole campaign

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `7f1f6922c`
**Predecessor:** sub-project 61, which closed `app/domain` — 38 floors.

## Goal

Cover the search package's only uncovered file, repair the three defects probed
while scoping it, **fix the harness gap that makes every full-text search path
in the application untestable**, and take a floor, which closes `app/search`
(`__init__.py` and `forms.py` are already at 100).

## Targets

From the full-suite JSON at the delivered tree (`app/search/routes.py`
**6.485%**, 181 statements — the lowest the campaign has taken):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `run_search` | `:22-228` | 152 | 106 | 258 |
| `retrieve_remote_post` | `:233-245` | 10 | 6 | 16 |
| **total** | | **162** | **112** | **274** |

Above the band, taken whole because `run_search` is one function and cannot be
split without splitting a single `if/elif` chain across rounds.

## THE HARNESS GAP, WHICH COMES FIRST

`Post.query.search(q)` and `PostReply.query.search(q)` are the point of this
module, and **neither can run in this suite**:

```
psycopg2.errors.UndefinedFunction: function parse_websearch(unknown) does not exist
```

`sqlalchemy_searchable` defines `parse_websearch` and its companions in
`sql_expressions`, which `make_searchable` emits through a `before_create` DDL
listener — so a database built by `create_all` has it and **a database built by
migrations does not**. This suite's database is built by migrations
(`run_tests.sh` runs `flask db upgrade`), so the function has never existed in
it, and `\df parse_websearch` returns no rows.

**The consequence is larger than this round**: every `.search()` call in the
application — community search, user search, this module — is currently
untestable, and any test that reaches one dies with a database error rather
than a failure anyone would read as a missing fixture.

**Fix: `tests/conftest.py` executes `sql_expressions` once, session-scoped,
after the migrations have run.** Verified: with it in place,
`Post.query.search('hello').count()` returns 0 instead of raising. This is the
campaign's first change to `conftest.py`, and it is declared here rather than
made quietly.

## THE THREE PRODUCTION CHANGES

### P1 — an unknown `search_for` is a 500

`run_search` builds `next_url` and `prev_url` inside the `posts` branch and
again inside the `comments` branch. `communities` and `people` return
redirects. **Anything else falls through to the render with both names
unbound**:

```
PROBE e1 exception: UnboundLocalError cannot access local variable 'next_url' where it is not associated with a value
```

`/search?q=hello&search_for=bogus` is a 500 on a public route.

**Fix:** `next_url` and `prev_url` are initialised beside `posts` and `replies`,
which are already initialised to None for exactly this reason. An unknown
`search_for` then renders the results page with nothing in it, which is what
the existing `posts = None` / `replies = None` pair already implies.

### P2 — a non-numeric `minimum_upvote` is a 500

```python
minimum_upvote = request.args.get('minimum_upvote', '')
...
if minimum_upvote:
    posts = posts.filter(Post.up_votes - Post.down_votes >= int(minimum_upvote))
```

**Probe:** `PROBE e2 exception: ValueError invalid literal for int() with base 10: 'lots'`.

**Fix:** read it with `type=int` at the top, as D726 and D777 did, and keep the
raw string only for the form field the template re-renders. A value that is not
a number then filters nothing rather than crashing.

### P3 — a crafted query injects parameters into the redirect

```python
return redirect(f'/communities?search={q}&language_id={language_id}')   # :187
return redirect(f'/instance/all/people?q={q}')                         # :190
```

**Probe** — `q` carrying an encoded `&`:

```
PROBE e5 redirect: 302 /communities?search=cats&language_id=99&language_id=0
```

The query string the user typed is spliced into a url unescaped, so it adds its
own parameters — here a second `language_id` that arrives before the route's
own. The targets are this site's own pages, so the blast radius is a confusing
result rather than an open redirect; it is still a url built by concatenation.

**Fix:** build both with `url_for`, which percent-encodes its values.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:89-97` | **THE ANONYMOUS NSFW BLOCK CONTRADICTS ITSELF.** It builds the same `exclude` / `only` / `include` chain the authenticated arm has, and then appends `posts = posts.filter(Post.nsfw == False)` **unconditionally**. So `nsfw=only` asks for `nsfw = true AND nsfw = false` and can only ever return nothing, and `include` is silently overridden. Either the chain is dead code or the trailing filter is wrong, and **which one is a product decision** about whether a logged-out reader may see NSFW results at all. | Registered, covered as behaviour: the tests record that an anonymous `only` returns nothing. |
| R2 | `:104`, `:159` | **`if q is not None:` can never be false** — `q` is `(request.args.get('q') or '').strip()`, a string always. So `.search('')` runs on every filter-only search. It is harmless (an empty search matches everything) but the guard reads as if it protects something. | A dead guard, D758's family; registered with a proof. |
| R3 | `:58` | **`SET work_mem = '100MB'` is executed on every search request**, against the connection the request happens to hold. | A performance decision, not a defect. Registered so it is not mistaken for test scaffolding. |
| R4 | `:127`, `:182` | **`has_prev and page != 1` again** — 1-based here, so redundant, exactly as D788 found in `app/domain`. Fact 316 already covers the shape. | Registered by reference, not re-proved. |

## Success criteria

- Both functions at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof — R2 is expected to produce two.
- **`app/search/routes.py` takes a floor** at the measured figure rounded down,
  which closes `app/search` — 39 floors.
- The three repairs land with pins inverted; `git diff --numstat` names exactly
  `app/search/routes.py`.
- `tests/conftest.py` gains the search-expression installation, with a comment
  naming why it is needed and what it mirrors.
- Suite green; floors checked with `&&`; a mutation pass over the module, per
  D602.
- Findings registered from **D791**; `tests/README.md` facts from **317**.
- `tests/test_zz_search_probe.py` deleted before delivery.

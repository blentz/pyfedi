# Sub-project 54: `app/feed/routes.py` Group C — the reading routes, and the module's floor

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `c8c85a0c`
**Predecessor:** sub-project 53, which closed Group B and took the module to
82.444 with five production repairs.

## Goal

Cover the module's remaining routes — `feed_list`, `show_feed`,
`get_all_child_feed_ids`, `feed_create_post` and `show_feed_rss` — repair the
three defects found in `feed_list`, and **take `app/feed/routes.py`'s first
coverage floor**, which closes the module.

## Targets

From the delivered tree's full-suite JSON at `c8c85a0c`:

| route | lines | missing statements | missing arcs |
|---|---|---|---|
| `feed_list` | `:402-433` | 12 | 6 |
| `show_feed` | `:435-579` | 33 | 22 |
| `get_all_child_feed_ids` | `:581-587` | 4 | 2 |
| `feed_create_post` | `:593-620` | 19 | 10 |
| `show_feed_rss` | `:751-805` | 6 | 6 |
| **total** | | **74** | **46** |

Below the campaign's 110-130 band, and taken at that size because it is what
remains: the module has no other uncovered route, and this round closes it.

## THE FACT THAT SHAPES THIS ROUND

**`feed_list` is twelve statements that nothing has ever executed, and all three
of its defects are in the first five lines.**

```python
user_id = int(request.args.get('user_id'))
community_id = int(request.args.get('community_id'))
current_feed_id = int(request.args.get('current_feed_id'))
user_feeds = Feed.query.filter_by(user_id=user_id).all()
```

**P1 — it serves any user's feeds to any logged-in caller, private ones
included.** The route is `@login_required` and nothing else: the `user_id` it
filters on comes from the query string, and the query has no `public` filter.

```
PROBE list status: 200
PROBE list body: <li><a class="dropdown-item" href="...">Owner secret feed</li>
```

That body is another user's **private** feed, fetched by a second account.

**P2 — a missing argument is a 500.** `int(None)`:

```
PROBE args exception: TypeError int() argument must be a string, a bytes-like
object or a real number, not 'NoneType'
```

**P3 — the feed title is interpolated into HTML unescaped.**

```
PROBE xss raw tag present: True
PROBE xss body tail: ...community_id=1"><img src=x onerror=alert(1)></li>
```

The title is user-supplied (the create form's `title` field), and the route
returns raw HTML that the caller's page splices into a dropdown.

**Why all three are one repair:** the route's parameters are its whole attack
surface, and the fix for P1 — take the acting user from the session rather than
the query string — removes the only way P3's payload reaches a third party.
They are repaired together, with a test each.

## THE THREE PRODUCTION CHANGES

### P1 — the acting user comes from the session

`Feed.query.filter_by(user_id=user_id)` becomes a query for
`current_user.id`. The query-string parameter stays in the *generated links*,
which is what it is for; it stops deciding whose feeds are listed.

### P2 — the three `int()` calls get defaults

`request.args.get(name, 0, type=int)`, which is the idiom `show_feed:459`
already uses in this file. A missing or non-numeric argument then reads 0
rather than raising.

### P3 — the title is escaped

`markupsafe.escape(feed.title)` at the interpolation. The route hand-builds
HTML, so the escape has to be explicit.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `feed_list:420`, `:427` | **The route hand-builds HTML in a Python f-string**, `<li>` without a closing `</li>` on the anchor, and returns it as a bare string rather than a template. P3 escapes the one interpolated value; the shape stays. | Moving it to a template is a UI change with its own review, not a coverage change. |
| R2 | `show_feed:485-494` | **The breadcrumb trail builds a namedtuple CLASS per entry and assigns attributes to it**, rather than instantiating. It works -- each iteration makes a fresh class -- but `existing_url` is initialised to `/f` and never appended to, so it is an accumulator that accumulates nothing. | Both are cosmetic given the flat url scheme; recorded so a later reader does not mistake either for a bug, and so a change to nested urls knows to look here. |
| R3 | `feed_create_post:599`, `:608`; `feed_copy:283` | **The same odd `FeedItem.query.join(Feed, FeedItem.feed_id == <id>)` in three places**, whose ON clause is a constant predicate rather than a join key. Sub-project 53 probed it and it does not duplicate rows, which is why it is registered rather than repaired. | A query that reads wrong and behaves right is a correctness risk on the next edit; registered with 53's measurement attached. |

## Success criteria

- The five routes at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- **`app/feed/routes.py` TAKES ITS FIRST FLOOR**, at the measured blended
  `percent_covered` rounded down: **29 floors**, `coverage_floors.ini` gaining
  exactly one line.
- The three repairs land with pins inverted; `git diff --numstat` names exactly
  `app/feed/routes.py`.
- No regression; suite green; floors checked with `&&`.
- A mutation pass over the five routes, scoped as D602 requires.
- Findings registered from **D723**; `tests/README.md` facts from **299**.

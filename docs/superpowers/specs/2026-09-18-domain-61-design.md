# Sub-project 61: `app/domain/routes.py` — closing the package

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `e37767dc5`
**Predecessor:** sub-project 60, which closed `app/topic` — 37 floors.

## Goal

Cover the domain package's only uncovered file — the domain page, its feed, the
two listings, and the block/unblock/ban/unban pair of pairs — repair the three
defects probed while scoping it, and **take a floor, which closes
`app/domain`** (`__init__.py` and `forms.py` are already at 100).

## Targets

From the full-suite JSON at the delivered tree (`app/domain/routes.py`
18.107%, 189 statements — the lowest-covered module the campaign has taken
whole so far):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `show_domain` | `:27-106` | 43 | 20 | 63 |
| `show_domain_rss` | `:110-165` | 43 | 18 | 61 |
| `domains` | `:169-189` | 13 | 4 | 17 |
| `domain_unblock` | `:242-260` | 11 | 4 | 15 |
| `domains_blocked_list` | `:194-209` | 10 | 2 | 12 |
| `domain_block` | `:224-237` | 8 | 2 | 10 |
| `domain_ban` | `:266-273` | 7 | 2 | 9 |
| `domain_unban` | `:279-285` | 6 | 2 | 8 |
| `unban_all` | `:215-219` | 4 | 0 | 4 |
| **total** | | **145** | **54** | **199** |

Above the 110-130 band, taken at that size because it is what the package has
left and this round closes it — sub-projects 55 and 60 gave the same reason.

## THE THREE PRODUCTION CHANGES

### P1 — a dot-less, non-numeric domain id is a 500

```python
if '.' in domain_id:
    domain = Domain.query.filter_by(name=domain_id, banned=False).first()
else:
    domain = Domain.query.get_or_404(domain_id)      # :33, and :115 in the feed
```

The route is `/d/<domain_id>` with no converter, so the id arrives as a string.
A value with a dot is read as a domain NAME; anything else goes to `get_or_404`,
which passes it to the database as a primary key.

**Probe:** `PROBE d1 exception: DataError (psycopg2.errors.InvalidTextRepresentation) invalid input syntax for type integer: "notanumber"`.

Both `show_domain` and `show_domain_rss` carry the same two lines, so both are
500s. The route is public and unauthenticated — any crawler following a
mangled link reaches it.

**Fix:** a non-numeric id is a 404, which is what `get_or_404` was reaching for.
The guard goes in both copies, since leaving one is the shape this campaign has
met six times.

### P2 — the feed's de-duplication leaves a half-built entry

```python
for post in posts:
    fe = fg.add_entry()
    fe.title(post.title)
    ...
    if post.url:
        if post.url in already_added:
            continue                       # :149
        ...
    fe.description(post.body_html)
    fe.guid(post.profile_id(), permalink=True)
    fe.author(name=post.author.user_name)
    fe.pubDate(post.created_at.replace(tzinfo=timezone.utc))
```

The entry is added to the feed BEFORE the duplicate check, and `continue` skips
everything after it — so a second post sharing a url produces an `<item>` with
a title and a link and no description, guid, author or date.

**Probe** — two posts, one url:

```
PROBE d2 status: 200
PROBE d2 entries: 2 guids: 1 authors: 0
```

**Fix: decide before adding the entry.** The seen-url check moves above
`fg.add_entry()`, so a duplicate produces no item rather than a broken one,
which is what the `already_added` set was built to do.

### P3 — `domain_unblock` crashes when htmx sends no current url

```python
if request.headers.get("HX-Request"):
    curr_url = request.headers.get("HX-Current-Url")
    if "/d/" in curr_url:                   # :255
```

**Probe:** `PROBE d3 exception: TypeError argument of type 'NoneType' is not iterable`.

**This is D756 exactly** — the same unguarded read of the same optional header,
repaired in `app/chat/routes.py` by sub-project 57 four rounds ago. **Fix takes
that round's decision**: an absent header takes the same branch as a `/d/` url,
so the caller is sent to the domain page rather than to `None`.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:271`, `:283` | **`if domain:` after `get_or_404` in `domain_ban` and `domain_unban`.** `get_or_404` raises rather than returning a falsy value, so the guard cannot be false — and the implicit `None` return it hides would be a 500, not a refusal. Two dead branches, D758's and D773's shape. | Registered with a proof, following both precedents; the round's diff stays the three repairs. |
| R2 | `:107-108`, `:164-165` | **Both routes end `else: abort(404)` reached only through the `if domain:` above** — live for the name lookup, which returns None for an unknown name, so unlike R1 these are reachable and covered. | Not a defect; recorded so the two `abort(404)`s are not confused with R1's dead ones. |
| R3 | `:131`, `:135` | **The feed's self link points at `/c/<id>/feed`** — a community url — while its alternate link points at `/d/<id>`. A reader following the self link gets someone else's page or a 404. | A one-character fix in a string this round does not otherwise touch, and the campaign repairs what it probes: this is registered with the line quoted so it is repaired with evidence rather than by eye. |
| R4 | `:29`, `:111` | **Both public routes carry `limiter.limit('60/minute')` as a context manager** rather than a decorator, which is unusual enough to be worth recording — it works, and the rate limiter is disabled in this suite (`disable_rate_limiter`), so nothing here measures it. | Not a defect. |

## Success criteria

- Every function at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof — R1 is expected to produce
  exactly two such arcs.
- **`app/domain/routes.py` takes a floor** at the measured figure rounded down,
  which closes `app/domain` — 38 floors.
- The three repairs land with pins inverted; `git diff --numstat` names exactly
  `app/domain/routes.py`.
- Suite green; floors checked with `&&`; a mutation pass over the module, per
  D602.
- Findings registered from **D784**; `tests/README.md` facts from **316**.
- `tests/test_zz_domain_probe.py` deleted before delivery.

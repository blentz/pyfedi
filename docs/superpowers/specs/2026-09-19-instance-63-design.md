# Sub-project 63: `app/instance/routes.py` — closing the package

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `5f4f8294b`
**Predecessor:** sub-project 62, which closed `app/search` and repaired the
full-text-search harness gap — 39 floors.

## Goal

Cover the instance package's only uncovered file — the instance list with its
filter algebra, the per-instance overview, people and posts pages, the bulk
follow importer, and the block/unblock pair — repair the defect probed while
scoping it, and **take a floor, which closes `app/instance`** (`util.py` is
already floored at 100).

## Targets

From the full-suite JSON at the delivered tree (`app/instance/routes.py`
16.573%, 242 statements):

| function | where | missing statements | missing arcs | total |
|---|---|---|---|---|
| `list_instances` | `:26-114` | 63 | 46 | 109 |
| `instance_posts` | `:241-324` | 54 | 22 | 76 |
| `instance_people` | `:129-172` | 29 | 18 | 47 |
| `instance_add_people` | `:208-237` | 12 | 13 | 25 |
| `instance_people_top` | `:176-201` | 15 | 4 | 19 |
| `instance_block` | `:329-341` | 9 | 2 | 11 |
| `instance_overview` | `:118-125` | 4 | 2 | 6 |
| `instance_unblock` | `:346-358` | 3 | 1 | 4 |
| **total** | | **189** | **108** | **297** |

Above the band, taken whole because it closes the package — the reason
sub-projects 55, 60 and 61 gave.

## THE ONE PRODUCTION CHANGE

### P1 — the unblock sends htmx to a page called `/None`

```python
if request.headers.get('HX-Request'):
    resp = make_response()
    resp.headers["HX-Redirect"] = request.headers.get('HX-Current-Url')
```

`HX-Current-Url` is optional. When it is absent the header is assigned `None`,
and werkzeug stringifies it:

```
PROBE f3 status: 200 HX-Redirect: 'None'
```

htmx then navigates the reader to a relative url named `None` — a 404 on this
site, after the unblock has already been written.

**This is D756's family in its third module** — `app/chat/routes.py` (D756) and
`app/domain/routes.py` (D786) both read the same optional header without a
guard. The difference is the failure mode: those two crashed, this one
silently sends the reader somewhere that does not exist, which is why no one
has reported it.

**Fix takes the same decision those rounds took**: an absent header falls back
to a page the route can name — here the instance overview, which is exactly
what `instance_block` twelve lines above already does.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:220-224` | **A CSV that is not UTF-8 is a 500.** `form.mastodon_csv.data.read().decode('utf-8')` with no guard: `PROBE f4 exception: UnicodeDecodeError 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte`. A Mastodon export is always UTF-8, so a file that is not one is almost certainly the wrong file — but the answer to the wrong file is a message, not a traceback. | Registered by decision: what a refusal should say to the user is a product choice. Covered as behaviour, asserting the exception. |
| R2 | `:146-156` | **The admin and non-admin branches of `instance_people` are identical except for `searchable=True`** — the silenced-instance filter and the instance filter are written out twice, verbatim. | A duplication, not a defect; registered so a mutation survivor there is read correctly. |
| R3 | `:249-255` | **`instance_posts` filters no `Post.private`**, while `app/search/routes.py` and `app/domain/routes.py` both exclude it. `Post.private` is the microblog marker rather than a privacy flag (`tests/factories.py`'s note), so including microblogs in an instance feed may well be deliberate. | Registered with the three call sites named, so the difference is a decision someone makes rather than a drift nobody noticed. |
| R4 | `:301-313` | **D731's namedtuple-class-per-entry breadcrumb shape, in a third module** (`app/feed/routes.py` D731, `app/topic/routes.py` D783). | Registered by reference. |
| R5 | `:99`, `:163`, `:199` | **`has_prev and page != 1`, 1-based here and therefore redundant** — fact 316's shape, in three more places. | Registered by reference, not re-proved. |

## Success criteria

- Every function at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- **`app/instance/routes.py` takes a floor** at the measured figure rounded
  down, which closes `app/instance` — 40 floors.
- P1 lands with its pin inverted; `git diff --numstat` names exactly
  `app/instance/routes.py`.
- Suite green; floors checked with `&&`; a mutation pass over the module, per
  D602.
- Findings registered from **D801**; `tests/README.md` facts from **319**.
- `tests/test_zz_instance_probe.py` deleted before delivery.

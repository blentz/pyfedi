# Sub-project 66: `app/tag/routes.py` — the tag page and its feed

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `cadf06a86`
**Predecessor:** sub-project 65, which closed `app/dev` — 44 floors.

## Goal

Cover the two reading surfaces of a hashtag — the HTML page at `/tag/<tag>` and
the RSS feed at `/tag/<tag>/feed` — and repair the three defects probed while
scoping them. An **interim floor** is taken; the package closes in 67 and 68.

## Why the package is split

`app/tag/routes.py` carries **209 missing statements and 106 missing arcs** at
11.517%, which is more than twice the band a single round has ever taken. The
split follows the module's own seams:

| round | functions | missing statements | missing arcs |
|---|---|---|---|
| **66 (this one)** | `show_tag`, `show_tag_rss` | 90 | ~45 |
| 67 | `tags`, `tags_blocked_list`, `tag_ban`, `tag_unban`, `tag_posts` | 77 | ~35 |
| 68 | `tag_cloud` | 42 | ~26 |

`app/tag/__init__.py` is already at 100.0, so 68 closes the package.

## Targets

From the full-suite JSON at the delivered tree (`app/tag/routes.py` 11.517%,
250 statements, 106 branches):

| function | where | missing statements | total with arcs |
|---|---|---|---|
| `show_tag` | `:25-117` | 51 | ~76 |
| `show_tag_rss` | `:120-170` | 39 | ~59 |
| **total** | | **90** | **~135** |

## THE PRIVATE-INSTANCE GATE, WHICH COMES FIRST

`show_tag` is decorated `@login_required_if_private_instance` and
`Site.private_instance` defaults to **True** (`app/models.py`), so an anonymous
request measures the decorator rather than the view. Every anonymous row here
sets `site.private_instance = False` first, which is what sub-project 60's
`_seed()` does for the same reason.

`show_tag_rss` carries **no** such decorator — see R1; that is the repo's
convention for RSS, not this module's mistake, and it is registered rather than
repaired.

## THE THREE PRODUCTION CHANGES

### P1 — page 2 of a tag page does not exist

```python
next_url = url_for('tag.show_tag', tag=tag, page=posts.next_num, ...)
prev_url = url_for('tag.show_tag', tag=tag, page=posts.prev_num, ...)
```

`tag` was rebound to the **Tag row** at `:32`. Flask's URL builder has nothing
but `str()` for a model, and Flask-SQLAlchemy's default `__repr__` answers
`<Tag 1>`.

**Probe** — one tag, 101 posts, so a second page genuinely exists:

```
PROBE k1 status: 200
PROBE k1 next_url: /tag/%3CTag%201%3E?page=2&category=
```

The link renders, resolves to no tag, and 404s. A tag with more than 100 posts
is readable only to its first page, and only the URL says why. **D736's shape
again**: an action whose sole observable is a value nobody checked.

**Fix:** pass `tag=tag.name`, which is the string the route's own converter
matches and the value `show_tag_rss`'s `rss_feed` line at `:112` already uses.

### P2 — the tag's RSS feed publishes microblogs the tag's page hides

`show_tag` filters `Post.private == False` (`:38`). `show_tag_rss` does not
(`:126`).

**Probe** — one microblog post carrying the tag:

```
PROBE k5 show_tag posts: 0
PROBE k8 body has entry: True
```

`Post.private` is the **microblog marker**, not a followers-only flag
(`tests/factories.py:294`), and its documented job is to keep ingested
microblogs off the discovery surfaces — "search, tags, domains, user profiles".
The page obeys that. The feed of the same page does not, so subscribing to a
tag's RSS returns content that browsing the tag will not show.

The closest sibling settles it: `show_domain_rss`
(`app/domain/routes.py:128-130`) filters `Community.private == False` **and**
`Post.private == False`; `show_topic_rss` filters `private is false` on the
community. `show_tag_rss` already filters `Community.private == False` at
`:130` and is missing only the post half.

**Fix:** add `Post.private == False` to the RSS query.

### P3 — two locals that are always `None`, and the four dead lines under them

```python
description = None
og_image = None
...
if og_image:
    fg.logo(og_image)
else:
    fg.logo(f"...{g.site.logo_152 ...}")
if description:
    fg.subtitle(description)
else:
    fg.subtitle(' ')
```

Neither name is ever assigned anything else, so both `if` arms are unreachable
and no test can reach them. They are the residue of a copy: in
`show_community_rss` (`app/community/routes.py:733-734`) the same two names are
filled from the community's description and image, and the branches are live.
Here the sources were dropped and the tests were not.

**Fix:** delete the two locals and both dead arms, keeping the `else` bodies —
the only ones that run — so the feed's logo and subtitle are unchanged. This is
the alternative to two pragmas, and it is the one that leaves no unexecuted
line behind to be re-explained by the next reader.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:120-121` | **The RSS feed has no `@login_required_if_private_instance`**, so on a private instance a tag's posts are readable anonymously through the feed. | Repo-wide: **no** RSS route carries it — `app/domain/routes.py:111`, `app/topic/routes.py:215`, `app/community/routes.py:706`, `app/user/routes.py:2205`, `app/main/routes.py:1219`. Fixing one and not the other five would be worse than registering all six. |
| R2 | `:90-93` | **`FeedItem.query.join(Feed, FeedItem.feed_id == fid)` is not a join**: the condition names no Feed column, so it is a cross join of every `Feed` row against the `FeedItem`s of one feed. The community ids come out right and come out N times, N being the number of feeds on the instance. | The duplicates change no answer — `IN` is a set test — so there is no observable to assert on, which is the campaign's bar for a repair. Registered as a cost, and it appears three times in this module (`:90`, `:279`, `:400`), so 67 and 68 meet it again. |
| R3 | `:74-76`, `:100` | **`category`/`category_id` are trusted as a pair**: `category=topic` with no `category_id` silently drops the filter, and `category=nonsense&category_id=1` renders the whole tag as though no category were asked for. | A request naming a category that does not exist gets the unfiltered page rather than a 404. That is a product decision about a query string nothing in the UI produces. |

## Success criteria

- Both functions at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- `app/tag/routes.py` takes an **interim** floor at the measured figure rounded
  down (45 floors; the package closes in 68).
- All three repairs land with pins inverted; `git diff --numstat` names exactly
  `app/tag/routes.py`.
- Suite green; floors checked with `&&`; a mutation pass over both functions,
  per D602.
- Findings registered from **D820**; `tests/README.md` facts from **322**.

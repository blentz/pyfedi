# Sub-project 68: `app/tag/routes.py` — the tag cloud, and closing the package

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `3dab9dc41`
**Predecessor:** sub-project 67, which took the lists, the ban pair and the post list — 45 floors.

## Goal

Cover `tag_cloud`, the last function in the module, repair the five defects
probed while scoping it, and **close `app/tag`**: `__init__.py` is already at
100, so a floor of 100 here completes the package.

## Targets

From the full-suite JSON at `3dab9dc41` (`app/tag/routes.py` 81.714%):

| function | where | missing statements |
|---|---|---|
| `tag_cloud` | `:251-347` | 42 |

## THE FIVE PRODUCTION CHANGES

### P1 — `?page=abc` is a 500

```python
page = int(request.args.get('page', 1))
```

```
PROBE k6 exception: ValueError invalid literal for int() with base 10: 'abc'
```

Every other function in this module reads the same value through
`request.args.get('page', 1, type=int)`, which answers the default for anything
that is not a number. This is the third time the campaign has met this exact
shape in this file's neighbourhood (D829, and sub-projects 60 and 62 before it).

**Fix:** `type=int`.

### P2 — a type that is not a type renders an empty cloud with a 200

```python
if type == 'community': ...
elif type == 'topic': ...
elif type == 'feed': ...
```

No `else`. `community_ids` stays `[]`, the query matches nothing, and the page
renders:

```
PROBE k9 status: 200
PROBE k9 title: Nonsense tags
```

A URL naming no category the route understands gets a page that looks like a
real, empty cloud rather than a refusal — and the title reflects the unknown
word straight back. (It is escaped by Jinja, so this is a correctness defect,
not an injection one; see R3.)

**Fix:** `abort(404)` on the else, which is what `get_or_404` already does one
line further in for each of the three types that ARE understood.

### P3 — the cloud counts tags on posts the tag page will not show

The counting query filters `Post.deleted == False` and nothing else about the
post:

```python
tags_query = db.session.query(Tag, db.func.count(Post.id).label('num_posts')). \
    filter(Tag.banned == False). \
    join(post_tag, ...).join(Post, ...). \
    filter(Post.community_id.in_(community_ids), Post.deleted == False). \
    group_by(Tag.id)
```

**Probe** — one post at `status=0` and one ingested microblog, each with its own
tag:

```
PROBE p3 tags: ['microtag', 'reviewingtag']
```

Both are counted. `show_tag` filters `Post.status > POST_STATUS_REVIEWING` and
`Post.private == False`, so **clicking either tag in the cloud leads to a page
with nothing on it**. The cloud is a navigation surface; a tag it offers that
goes nowhere is the defect.

**Fix:** the two filters `show_tag` applies, in both the counting query and the
co-occurrence subquery — they answer the same question and have to agree.

### P4 — the cloud of a banned community, and of a private one

The topic branch filters `banned is false` in its SQL. The community branch does
not filter anything:

```python
if type == 'community':
    community = Community.query.get_or_404(category_id)
    community_ids.append(community.id)
```

**Probes:**

```
PROBE p1 status: 200      PROBE p1 tags: ['solarstorm']   (community.private = True)
PROBE p2 status: 200      PROBE p2 tags: ['solarstorm']   (community.banned = True)
```

A private community is invite-only and is real access control. Its tag cloud is
a list of what its members are talking about, served here to anyone with the
id — **D828 again, one function along**, and found the same way.

**Fix:** join `Community` in the counting query and apply what `show_tag`
applies — `Community.banned == False`, and `Community.private == False` unless
the reader is a member. Doing it in the query rather than in the three branches
covers all three category types at once, including the feed branch, which never
filtered either column.

### P5 — the co-occurrence subquery has to be filtered with it

```python
posts_with_tag1 = db.session.query(post_tag.c.post_id).filter(...).join(Post, ...).filter(
    Post.community_id.in_(community_ids),
    Post.deleted == False
).subquery()
```

Same gaps as P3 and P4, so the relationship weights are computed over posts the
cloud is no longer counting. `PROBE p7 relationships: {}` confirms the `deleted`
half already works, which is what makes the missing halves visible rather than
theoretical.

**Fix:** the same filters, applied to the same joins.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:296-300` | **Both pagination links hardcode `view='list'`**, so a reader paging the CLOUD view is switched to the list view by the act of paging: `PROBE p5 view: cloud`, `PROBE p5 next_url: ...?view=list&page=2`. | It reads as deliberate — paging a word cloud is not a meaningful gesture, and the list is where pages exist. Registered rather than "fixed" into something nobody asked for. Settles 67's R1 for this function: the link keeps `type` and `category_id` and was never dropping a filter. |
| R2 | `:283-286` | **`FeedItem.query.join(Feed, FeedItem.feed_id == fid)`** — the third and last copy of D822's non-join. | Unchanged from 66: the duplicates change no answer, so there is no observable to assert on. Now registered at all three sites (`:90`, `:283`, `:400`), which is what 66 said this round would confirm. |
| R3 | `:344` | **`title=f'{type.capitalize()} tags'`** puts a URL segment in the page title. Jinja autoescapes it, so it is not injection; with P2 in place an unknown type never reaches it at all. | Named only so the next reader does not have to re-derive that it is safe. |
| R4 | `:317-341` | **The co-occurrence block is one query per tag** — up to 50 per request, each with a subquery. | A performance shape, not a correctness one, and the round that rewrites it should have a benchmark. Registered with the campaign's other N+1 findings. |

## Success criteria

- `tag_cloud` at `[]`/`[]` on the **full-suite** run.
- **`app/tag/routes.py` takes a floor of 100**, which closes `app/tag` — 45
  floors, all three files in the package complete.
- All five repairs land with pins inverted; `git diff --numstat` names exactly
  `app/tag/routes.py`.
- Suite green; floors checked with `&&`; a mutation pass over `tag_cloud`, with
  every anchor checked for uniqueness before the pass runs — D833.
- **Re-measure the suite's wall time** rather than assuming it: D831 recorded
  439s and 655s for near-identical trees, a spread wider than the margin D817
  left under `pytest.ini`'s 1200s budget.
- Findings registered from **D834**; `tests/README.md` facts from **325**.
